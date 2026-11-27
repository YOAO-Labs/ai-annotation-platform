"""Run the module-data LOGIN-role acceptance against owned local resources.

Usage: apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
    --output /tmp/aap-module-data-acceptance.json

The JSON report is paired with sibling JUnit XML and collection manifest files.
Only this invocation's PostgreSQL container and LocalResources test topology
are created or removed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import UUID, uuid4
import shutil
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

if __package__:
    from .contract_schemas import contract_schema_sha256, load_contract_schema
    from .grant_sql import read_psql_grant_script
else:
    from contract_schemas import contract_schema_sha256, load_contract_schema
    from grant_sql import read_psql_grant_script


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

CONTAINER_LABEL = "org.aap.acceptance"
CONTAINER_LABEL_VALUE = "module-data-boundary"
CONTAINER_ID_LABEL = "org.aap.acceptance-id"
POSTGRES_IMAGE = "postgres:16-alpine"
INFERENCE_REGISTRY_INGRESS_ROLE = "yoao_inference_registry_ingress_client"
INFERENCE_ROUTING_ROLE = "yoao_inference_routing_client"
INFERENCE_OWNER_PROFILES = {
    "INFERENCE_ENDPOINT_SETUP": ("setup", "yoao_inference_endpoint_setup_client"),
    "INFERENCE_ENDPOINT_HEALTH_OBSERVATION": (
        "health_observation",
        "yoao_inference_endpoint_health_client",
    ),
    "INFERENCE_ENDPOINT_CATALOG_FALLBACK": (
        "catalog_fallback",
        "yoao_inference_endpoint_catalog_client",
    ),
    "INFERENCE_ENDPOINT_STATS": ("stats", "yoao_inference_endpoint_stats_client"),
    "INFERENCE_ENDPOINT_BACKEND_READ": (
        "backend_read",
        "yoao_inference_endpoint_read_client",
    ),
    "INFERENCE_ENDPOINT_LIFECYCLE": (
        "lifecycle",
        "yoao_inference_endpoint_lifecycle_client",
    ),
    "INFERENCE_ENDPOINT_EXECUTION": (
        "execution",
        "yoao_inference_endpoint_execution_client",
    ),
    "INFERENCE_BACKEND_STATS": ("backend_stats", "yoao_inference_backend_stats_client"),
    "INFERENCE_GPU_HEALTH_ENDPOINT": (
        "gpu_health_observation",
        "yoao_inference_gpu_health_endpoint_client",
    ),
    "INFERENCE_GPU_CONTROL_ENDPOINT": (
        "gpu_control",
        "yoao_inference_gpu_control_endpoint_client",
    ),
    "INFERENCE_GPU": ("gpu_authority", "yoao_inference_gpu_authority_client"),
    "INFERENCE_GPU_COLLECTOR": ("gpu_collector", "yoao_inference_gpu_collector_client"),
}
VIDEO_EXECUTION_ROLE = "yoao_video_execution_client"
IDENTITY_API_CONSUMER_ROLE = "identity_api_consumer"
NOTIFICATION_PREFERENCES_ROLE = "yoao_notification_preferences_client"
NOTIFICATION_APPEND_ROLE = "yoao_notification_append_client"
NOTIFICATION_INBOX_ROLE = "yoao_notification_inbox_query_client"
NOTIFICATION_DELIVERY_READ_ROLE = "yoao_notification_delivery_read_client"
JOBS_LIST_QUERY_ROLE = "yoao_jobs_list_query_client"
JOBS_LIFECYCLE_ROLE = "yoao_jobs_lifecycle_client"
JOBS_DISPATCH_RELAY_ROLE = "yoao_jobs_dispatch_relay_client"
JOBS_PUBLICATION_ROLE = "yoao_jobs_publication_client"
EXPORT_EXECUTION_ROLE = "yoao_export_execution_client"
IDENTITY_EMBEDDED_API_CONSUMER_ROLE = "identity_embedded_api_consumer"
IDENTITY_ISSUER_FACT_FUNCTIONS = (
    "yoao_identity_v1.read_actor_facts(uuid)",
    "yoao_identity_v1.lock_accounts_share(uuid[],boolean)",
    "yoao_identity_v1.lock_accounts_update(uuid[],boolean)",
    "yoao_identity_v1.lock_accounts_key_share(uuid[],boolean)",
    "yoao_identity_v1.check_accepted_api_key(uuid,uuid,text[])",
    "yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid)",
    "yoao_identity_v1.is_jti_revoked(uuid)",
    "yoao_identity_v1.get_credential_generation(uuid)",
)
IDENTITY_AVATAR_FUNCTIONS = (
    "yoao_identity_v1.reserve_avatar_upload(uuid,text,jsonb,text,integer)",
    "yoao_identity_v1.confirm_avatar_upload_version(uuid,text,jsonb,text,integer,text)",
    "yoao_identity_v1.avatar_upload_pending_matches(uuid,text,jsonb,text,integer,text)",
    "yoao_identity_v1.read_ready_avatar_upload(text)",
    "yoao_identity_v1.list_avatar_retirements(integer)",
    "yoao_identity_v1.begin_avatar_retirement(text)",
    "yoao_identity_v1.finish_avatar_retirement(text,jsonb,text,integer,text)",
    "yoao_identity_v1.read_avatar_binding_history()",
    "yoao_identity_v1.avatar_saved_target_referenced(uuid)",
)
IDENTITY_AVATAR_INVENTORY_FUNCTIONS = (
    "yoao_identity_v1.inventory_avatar_uploads(text,integer)",
    "yoao_identity_v1.usage_avatar_uploads(text[])",
)
SAVED_SOURCE_ROLE = "yoao_asset_source_saved_io_client"
STORAGE_MIGRATION_CONTROL_ROLE = "yoao_storage_migration_control_client"
SAVED_SOURCE_FUNCTIONS = (
    "yoao_assets_v1.saved_io_connector(uuid,integer,jsonb)",
    "yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb)",
    "yoao_assets_v1.saved_io_allowlist(jsonb)",
    "yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean)",
)
LEASED_IMPORT_FUNCTIONS = (
    "yoao_assets_v1.save_observed_dataset_import_manifest(uuid,uuid,jsonb)",
    "yoao_assets_v1.begin_dataset_import_original(uuid,uuid,jsonb)",
    "yoao_assets_v1.open_dataset_import_original(uuid,uuid,bigint,uuid,uuid)",
    "yoao_assets_v1.record_import_original_prepared(jsonb,jsonb)",
    "yoao_assets_v1.mark_import_original_writing(jsonb)",
    "yoao_assets_v1.record_import_original_written(jsonb,jsonb)",
    "yoao_assets_v1.fail_import_original(jsonb,text,text,jsonb)",
)
PRIVATE_RUNTIME_FUNCTIONS = (
    "yoao_assets_v1.capture_storage_connection(uuid,uuid,text)",
    "yoao_assets_v1.begin_operator_auxiliary(jsonb)",
    "yoao_assets_v1.operator_item_original_material(uuid,uuid)",
    "yoao_assets_v1.capture_native_pyramid_write_io(uuid,uuid,bigint,uuid,text)",
    "yoao_assets_v1.capture_native_pyramid_read_io(uuid,uuid,jsonb)",
    "yoao_assets_v1.capture_native_pyramid_retirement_io(uuid,uuid,uuid)",
    "yoao_settings_v1.mail_delivery_policy(jsonb)",
    "yoao_settings_v1.claim_smtp_test()",
    "yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)",
    "yoao_insights_v1.refresh_user_perf_daily()",
    "yoao_jobs_v1.claim_dispatches(jsonb)",
    "yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)",
    "yoao_inference_backend_endpoint_v1.open_execution_endpoint(text,uuid,uuid,text)",
    "yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)",
    "yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)",
    "yoao_inference_registry_ingress_v1.registry_by_url(text)",
    "yoao_inference_registry_ingress_v1.registry_candidates(text)",
    *SAVED_SOURCE_FUNCTIONS,
    *LEASED_IMPORT_FUNCTIONS,
)
JOBS_DOMAIN_QUERY_FUNCTIONS = (
    "yoao_jobs_v1.lock_cross_frame_singleflight(uuid,text)",
    "yoao_jobs_v1.active_cross_frame_job(uuid,text)",
    "yoao_jobs_v1.cross_frame_job_page(uuid,text,integer)",
    "yoao_jobs_v1.cross_frame_job_count(uuid,text)",
    "yoao_jobs_v1.latest_batch_prediction_times(uuid[])",
    "yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid)",
)
JOBS_ROW_LOCK_FUNCTIONS = (
    "yoao_jobs_v1.read_job_for_update(uuid)",
    "yoao_jobs_v1.read_job_for_share_nowait(uuid)",
    "yoao_jobs_v1.read_job_for_update_nowait(uuid)",
)
JOBS_LIFECYCLE_FUNCTIONS = (
    "yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)",
    "yoao_jobs_v1.read_job_execution_snapshot(uuid)",
    "yoao_jobs_v1.read_job(uuid)",
    "yoao_jobs_v1.jobs_by_execution_id(text)",
    "yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)",
    "yoao_jobs_v1.apply_lifecycle(uuid,jsonb)",
    "yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)",
    "yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)",
    "yoao_jobs_v1.delete_batch_predict_jobs(uuid)",
    "yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])",
)
JOBS_LIST_QUERY_FUNCTIONS = (
    "yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)",
    "yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)",
)
NOTIFICATION_DELIVERY_READ_FUNCTIONS = (
    "yoao_notifications_v1.read_notification(uuid)",
    "yoao_notifications_v1.find_terminal_notification(uuid,text,uuid)",
    "yoao_notifications_v1.delivery_job_scopes(uuid[])",
    "yoao_notifications_v1.delivery_member_pairs(jsonb)",
    "yoao_notifications_v1.delivery_reviewer_pairs(jsonb)",
    "yoao_notifications_v1.delivery_owner_pairs(jsonb)",
    "yoao_notifications_v1.delivery_super_pairs(jsonb)",
)
NOTIFICATION_INBOX_FUNCTIONS = (
    "yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer)",
    "yoao_notifications_v1.inbox_total_count_scoped(uuid,text)",
    "yoao_notifications_v1.inbox_unread_count_scoped(uuid,text)",
    "yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)",
    "yoao_notifications_v1.inbox_total_count(uuid,text)",
    "yoao_notifications_v1.inbox_unread_count(uuid,text)",
)
NOTIFICATION_INBOX_PRIVATE_HELPER = (
    "yoao_notifications_v1._visible_inbox_rows(uuid,text)"
)
FROZEN_OWNER_NAMESPACES = {
    "yoao_assets_v1": "aap_assets_v1",
    "yoao_audit_v1": "aap_audit_v1",
    "yoao_diagnostics_v1": "aap_diagnostics_v1",
    "yoao_export_v1": "aap_export_v1",
    "yoao_identity_accounts_v1": "aap_identity_accounts_v1",
    "yoao_identity_admin_passwords_v1": "aap_identity_admin_passwords_v1",
    "yoao_identity_briefs_v1": "aap_identity_briefs_v1",
    "yoao_identity_deactivation_v1": "aap_identity_deactivation_v1",
    "yoao_identity_deletions_v1": "aap_identity_deletions_v1",
    "yoao_identity_directory_v1": "aap_identity_directory_v1",
    "yoao_identity_fixture_v1": "aap_identity_fixture_v1",
    "yoao_identity_groups_v1": "aap_identity_groups_v1",
    "yoao_identity_invitations_v1": "aap_identity_invitations_v1",
    "yoao_identity_keys_v1": "aap_identity_keys_v1",
    "yoao_identity_mail_v2": "aap_identity_mail_v2",
    "yoao_identity_offboarding_v1": "aap_identity_offboarding_v1",
    "yoao_identity_platform_roles_v1": "aap_identity_platform_roles_v1",
    "yoao_identity_recovery_v1": "aap_identity_recovery_v1",
    "yoao_identity_sessions_v1": "aap_identity_sessions_v1",
    "yoao_identity_suspensions_v1": "aap_identity_suspensions_v1",
    "yoao_identity_v1": "aap_identity_v1",
    "yoao_inference_backend_endpoint_v1": "aap_inference_backend_endpoint_v1",
    "yoao_inference_backend_probe_v1": "aap_inference_backend_probe_v1",
    "yoao_inference_gpu_caller_v1": "aap_inference_gpu_caller_v1",
    "yoao_inference_gpu_collector_v1": "aap_inference_gpu_collector_v1",
    "yoao_inference_gpu_v1": "aap_inference_gpu_v1",
    "yoao_inference_health_data_v1": "aap_inference_health_data_v1",
    "yoao_inference_health_observation_v1": "aap_inference_health_observation_v1",
    "yoao_inference_pipelines_v1": "aap_inference_pipelines_v1",
    "yoao_inference_registry_ingress_v1": "aap_inference_registry_ingress_v1",
    "yoao_inference_routing_v1": "aap_inference_routing_v1",
    "yoao_inference_topology_v1": "aap_inference_topology_v1",
    "yoao_inference_v1": "aap_inference_v1",
    "yoao_insights_v1": "aap_insights_v1",
    "yoao_jobs_v1": "aap_jobs_v1",
    "yoao_labeling_class_query_v1": "aap_labeling_class_query_v1",
    "yoao_labeling_v1": "aap_labeling_v1",
    "yoao_learning_v1": "aap_learning_v1",
    "yoao_notifications_v1": "aap_notifications_v1",
    "yoao_settings_v1": "aap_settings_v1",
    "yoao_workspace_configuration_v1": "aap_workspace_configuration_v1",
    "yoao_workspace_directory_query_v1": "aap_workspace_directory_query_v1",
    "yoao_workspace_fixture_v1": "aap_workspace_fixture_v1",
    "yoao_workspace_project_query_v1": "aap_workspace_project_query_v1",
    "yoao_workspace_template_query_v1": "aap_workspace_template_query_v1",
    "yoao_workspace_v1": "aap_workspace_v1",
}
PROJECTED_GUCS = {
    "aap.identity_offboarding_prepare": "yoao.identity_offboarding_prepare",
    "aap.mask_gc_write_permit": "yoao.mask_gc_write_permit",
    "aap.restore_training_storage": "yoao.restore_training_storage",
}
NAMESPACE_BODY_CALLERS = {
    "public.capture_fixture_native_export",
    "public.capture_fixture_native_export_output",
    "public.fixture_export_execution_retired",
    "public.fixture_native_export_cleanup_eligible",
    "public.guard_fixture_export_job_identity",
    "public.record_fixture_export_execution",
    "public.retire_fixture_export_execution",
    "public.sync_gpu_backend_membership",
    "yoao_fixture_v1.guard_dataset_job_anchor",
    "yoao_fixture_v1.guard_jobs_anchor",
    "yoao_fixture_v1.retire_auxiliary_original",
    "yoao_inference_pipelines_v1.pipeline_step",
    "yoao_jobs_v1._async_job_list_rows",
    "yoao_notifications_v1._visible_inbox_rows",
    "yoao_notifications_v1.delivery_owner_pairs",
    "yoao_notifications_v1.delivery_super_pairs",
    "yoao_workspace_configuration_v1._class_rename_project_row",
    "yoao_workspace_configuration_v1._guide_asset_read_project_row",
    "yoao_workspace_membership_v1.apply_membership_command",
    "yoao_workspace_membership_v1.prepare_membership_command",
    "yoao_workspace_owner_transfer_v1.transfer_project_owner",
    "yoao_workspace_v1.delete_project",
    "yoao_workspace_v1.unlink_dataset",
}
KEY_USAGE_BODY_PATCHES = {
    "yoao_identity_v1.confirm_api_key_principal(uuid,text)": (
        ("lock", 1),
        ("write", 1),
    ),
    "yoao_identity_keys_v1.execute_key_command(jsonb)": (
        ("projection", 4),
        ("rotate", 1),
    ),
    "yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)": (
        ("projection", 1),
    ),
}
FUNCTION_BODY_ARTIFACTS = {
    "yoao_identity_briefs_v1._failure()": "0249_identity_briefs.sql",
    "yoao_identity_briefs_v1._has_exact_keys(jsonb,text[])": "0249_identity_briefs.sql",
    "yoao_identity_briefs_v1.briefs_policy()": "0249_identity_briefs.sql",
    "yoao_identity_briefs_v1.execute_briefs_command(jsonb)": "0249_identity_briefs.sql",
    "yoao_identity_keys_v1.key_policy()": "0236_identity_api_keys.sql",
    "yoao_identity_keys_v1.execute_key_command(jsonb)": "0236_identity_api_keys.sql",
    "yoao_identity_v1.confirm_api_key_principal(uuid,text)": "0235_identity_admission.sql",
    "yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)": "0248_identity_offboarding.sql",
    "yoao_labeling_class_query_v1.attribute_rows(uuid)": "0266_labeling_class_query.sql",
    "yoao_labeling_class_query_v1.class_counts(uuid)": "0266_labeling_class_query.sql",
    "yoao_labeling_class_query_v1.class_query_policy()": "0266_labeling_class_query.sql",
    "yoao_inference_pipelines_v1.pipeline_step(jsonb,text,jsonb,text[],text[])": "0271_inference_pipelines.sql",
    "yoao_workspace_directory_query_v1._has_exact_keys(jsonb,text[])": "0263_workspace_directory_queries.sql",
    "yoao_workspace_directory_query_v1.directory_query_policy()": "0263_workspace_directory_queries.sql",
    "yoao_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)": "0263_workspace_directory_queries.sql",
    "yoao_workspace_project_query_v1._has_exact_keys(jsonb,text[])": "0261_workspace_project_queries.sql",
    "yoao_workspace_project_query_v1.project_query_policy()": "0261_workspace_project_queries.sql",
    "yoao_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamp with time zone,timestamp with time zone,bigint,integer)": "0269_workspace_guide_assets.sql",
    "yoao_workspace_project_query_v1.project_search_member_ids(uuid,text)": "0261_workspace_project_queries.sql",
    "yoao_workspace_project_query_v1.project_search_scope(uuid,text)": "0261_workspace_project_queries.sql",
    "yoao_workspace_template_query_v1._has_exact_keys(jsonb,text[])": "0259_workspace_template_queries.sql",
    "yoao_workspace_template_query_v1._template_row_json(public.project_templates)": "0259_workspace_template_queries.sql",
    "yoao_workspace_template_query_v1.template_query_policy()": "0259_workspace_template_queries.sql",
    "yoao_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)": "0259_workspace_template_queries.sql",
    "yoao_diagnostics_v1.detach_dataset_task_references(uuid,uuid)": "0203_dataset_unlink_assets.sql",
    "yoao_diagnostics_v1.detach_project_references(uuid)": "0198_project_owned_cleanup_contract.sql",
    "yoao_insights_v1.clear_project_task_views(uuid)": "0198_project_owned_cleanup_contract.sql",
    "yoao_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)": "0300_video_deletion_retirement.sql",
    **{
        signature: "0463_native_pyramid_retirement.sql"
        for signature in (
            "yoao_assets_v1._native_pyramid_retirement_policy_valid(jsonb)",
            "yoao_assets_v1._native_pyramid_retirement_grace(jsonb)",
            "yoao_assets_v1._native_pyramid_retirement_target_valid(jsonb,uuid)",
            "yoao_assets_v1.guard_native_pyramid_retirement_target()",
            "yoao_assets_v1.guard_native_pyramid_retirement_page()",
            "yoao_assets_v1.record_native_pyramid_retirement()",
            "yoao_assets_v1.preserve_native_pyramid_retirement_grace()",
            "yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)",
        )
    },
    **{
        signature: "0463_native_pyramid_reads_retirement.sql"
        for signature in (
            "yoao_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)",
            "yoao_assets_v1._cancel_image_source_work(text,uuid,text)",
            "yoao_assets_v1.cancel_image_media(uuid,uuid,uuid)",
            "yoao_assets_v1._image_enqueue(uuid,uuid,jsonb,jsonb,boolean)",
            "yoao_assets_v1._image_pyramid_summary(jsonb)",
        )
    },
    **{
        signature: "0464_native_pyramid_saved_targets.sql"
        for signature in (
            "yoao_assets_v1._native_pyramid_output_fixture_owned(uuid)",
            "yoao_assets_v1._image_saved_output_target(uuid,integer,uuid,boolean)",
            "yoao_assets_v1._image_saved_output_prefix(uuid,integer,uuid,boolean)",
            "yoao_assets_v1._image_pyramid_projection(jsonb,integer,text)",
            "yoao_assets_v1.allocate_native_pyramid_output(uuid,bigint,uuid,uuid,integer)",
            "yoao_assets_v1.guard_native_pyramid_read_permit()",
            "yoao_assets_v1.begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid)",
            "yoao_assets_v1.complete_native_pyramid_read(uuid,uuid)",
            "yoao_assets_v1._native_pyramid_retirement_claimable(uuid)",
            "yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer)",
            "yoao_assets_v1._native_pyramid_retirement_page_items(uuid,uuid[])",
            "yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer)",
            "yoao_assets_v1.write_native_pyramid_member(uuid,text,text)",
            "yoao_assets_v1.abandon_native_pyramid_member(uuid,text)",
            "yoao_assets_v1.verify_native_pyramid_member(uuid)",
            "yoao_assets_v1.resolve_fixture_native_cache(text)",
            "yoao_assets_v1.legacy_pyramid_gc_eligible(text,uuid,integer)",
        )
    },
    **{
        signature: "0464_native_storage_capture.sql"
        for signature in (
            "yoao_assets_v1._native_pyramid_target_destination(uuid,text,text,text)",
            "yoao_assets_v1.capture_native_pyramid_write_io(uuid,uuid,bigint,uuid,text)",
            "yoao_assets_v1.capture_native_pyramid_read_io(uuid,uuid,jsonb)",
            "yoao_assets_v1.capture_native_pyramid_retirement_io(uuid,uuid,uuid)",
        )
    },
    "yoao_assets_v1.guard_fixture_native_target_output()": "0461_fixture_native_pyramid_target.sql",
    "yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)": "0467_workspace_membership_namespace.sql",
    **{
        signature: "0467_workspace_membership_namespace.sql"
        for signature in (
            "yoao_workspace_membership_v1.membership_policy()",
            "yoao_workspace_membership_v1.prepare_membership_command(jsonb)",
            "yoao_workspace_membership_v1.apply_membership_command(jsonb)",
        )
    },
    "yoao_assets_v1.guard_import_original_verification()": "0453_import_original_verification.sql",
    "yoao_assets_v1._import_verification_scope(jsonb,boolean)": "0453_import_original_verification.sql",
    "yoao_assets_v1.reserve_import_original_read(jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.cancel_unstarted_import_original_read(jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.begin_import_original_verification(jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.record_import_original_read(jsonb,jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.finish_import_original_verification(jsonb,jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.recover_import_original_verification(jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.status_import_original_verification(jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.resume_import_original_verification(jsonb)": "0453_import_original_verification.sql",
    "yoao_assets_v1.complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)": "0453_import_original_verification.sql",
    "yoao_jobs_v1.retire_job_publications(uuid,text[])": "0451_jobs_publication_retirement.sql",
    "yoao_inference_routing_v1.video_execution_scope_step(text,text)": "0450_video_candidate_status_restore.sql",
    "yoao_jobs_v1._video_restore_route_snapshot(bytea,text)": "0450_video_candidate_status_restore.sql",
    "yoao_jobs_v1._video_execution_route_step(text,text)": "0405_video_tracker_asset_candidates.sql",
    "yoao_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb)": "0449_image_backfill_purpose.sql",
    "yoao_assets_v1.fail_image_media(uuid,bigint,uuid,text,text)": "0449_image_backfill_purpose.sql",
    "yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)": "0433_project_video_retirement.sql",
    "yoao_assets_v1.lock_dataset_upload(uuid,uuid,uuid)": "0424_upload_publication_identity.sql",
    "yoao_labeling_v1.e2e_fixture_command(jsonb)": "0362_labeling_e2e_fixture_writes.sql",
    "yoao_labeling_v1.prepare_filtering_rendering(jsonb)": "0367_labeling_filtering_rendering.sql",
    "yoao_jobs_v1.prepare_dispatch(jsonb)": "0368_jobs_dispatch_ledger.sql",
    "yoao_jobs_v1.claim_dispatches(jsonb)": "0368_jobs_dispatch_ledger.sql",
    "yoao_jobs_v1.finish_dispatch(jsonb)": "0368_jobs_dispatch_ledger.sql",
    "yoao_jobs_v1.dispatch_status(uuid)": "0368_jobs_dispatch_ledger.sql",
    "yoao_jobs_v1._video_execution_settled(uuid,uuid,text,text,uuid)": "0409_video_execution_settlement.sql",
    "yoao_jobs_v1.video_execution_step(text,text)": "0411_video_execution_physical_closure.sql",
    "yoao_inference_topology_v1.registry_facts(uuid)": "0369_inference_topology.sql",
    "yoao_inference_topology_v1.registry_auth_snapshot(uuid)": "0410_inference_managed_auth.sql",
    "yoao_inference_topology_v1.patch_registry_auth_cas(uuid,text,text,bigint)": "0410_inference_managed_auth.sql",
    "yoao_inference_topology_v1.lock_repair_model_rows(uuid,uuid,uuid[])": "0386_inference_repair_model_lock.sql",
    "yoao_inference_topology_v1.lock_plugin_registry(uuid)": "0393_plugin_registry_admission_fence.sql",
    "yoao_inference_topology_v1.prepare_plugin_installation(text)": "0513_inference_plugin_direct_mode.sql",
    "yoao_inference_topology_v1.plugin_installation(uuid)": "0388_inference_plugin_installation.sql",
    "yoao_inference_topology_v1.plugin_installations()": "0388_inference_plugin_installation.sql",
    "yoao_inference_topology_v1.plugin_installation_evidence(uuid)": "0388_inference_plugin_installation.sql",
    "yoao_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)": "0513_inference_plugin_direct_mode.sql",
    "yoao_inference_topology_v1.authorize_plugin_routing_replacement(uuid,integer,uuid,text,text,text)": "0513_inference_plugin_direct_mode.sql",
    "yoao_inference_routing_v1._tracker_scopes_physically_retired(uuid)": "0412_plugin_tracker_retirement_order.sql",
    "yoao_inference_routing_v1._guard_disabled_plugin_tracker_scope()": "0412_plugin_tracker_retirement_order.sql",
    "yoao_inference_topology_v1.plugin_asset_approval(uuid)": "0403_plugin_asset_approval.sql",
    "yoao_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text)": "0513_inference_plugin_direct_mode.sql",
    "yoao_inference_topology_v1.create_manual_registry(uuid,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_topology_v1.patch_registry(uuid,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_topology_v1.delete_registry(uuid,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_registry_ingress_v1.tag_seed_registry(uuid,text)": "0388_inference_plugin_ingress.sql",
    "yoao_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(uuid,text)": "0457_inference_registry_seed_marker_revision.sql",
    "yoao_inference_pipelines_v1.frozen_snapshot_uses_deployment(uuid,uuid)": "0389_inference_deployment_usage.sql",
    "yoao_labeling_v1.retryable_deployment_usage(uuid,uuid,uuid)": "0389_labeling_deployment_usage.sql",
    "yoao_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid,uuid,text,text)": "0371_inference_backend_endpoint_auth_revision.sql",
    "yoao_inference_backend_endpoint_v1._endpoint_transport_identity(jsonb)": "0394_endpoint_routing_transport_identity.sql",
    "yoao_inference_backend_endpoint_v1.open_execution_endpoint(text,uuid,uuid,text)": "0403_plugin_asset_approval.sql",
    "yoao_inference_backend_endpoint_v1.open_installation_terminal_endpoint(uuid,uuid,text,text,text)": "0399_plugin_terminal_hold.sql",
    "yoao_inference_backend_endpoint_v1.backend_endpoint_policy()": "0399_plugin_terminal_hold.sql",
    "yoao_inference_routing_v1._routing_registry_before_change()": "0399_plugin_terminal_hold.sql",
    "yoao_inference_gpu_v1.gpu_authority_step(text,text)": "0399_plugin_terminal_hold.sql",
    "yoao_inference_gpu_v1.gpu_plugin_terminal_proof_valid(uuid,jsonb)": "0391_gpu_plugin_transition.sql",
    "yoao_inference_gpu_v1.gpu_plugin_prepared_cancellation_valid(uuid,jsonb)": "0392_plugin_prepared_cancellation.sql",
    "yoao_inference_gpu_v1.gpu_plugin_retirement_claim_valid(uuid,jsonb)": "0395_plugin_gpu_retirement.sql",
    "yoao_inference_gpu_v1.gpu_plugin_retirement_closure(uuid,text,bigint)": "0395_plugin_gpu_retirement.sql",
    "public.sync_gpu_backend_membership()": "0433_gpu_membership_scalar_guard.sql",
    "yoao_inference_gpu_caller_v1.gpu_caller_policy()": "0369_inference_gpu.sql",
    "yoao_inference_gpu_collector_v1.gpu_collector_policy()": "0369_inference_gpu.sql",
    "yoao_inference_gpu_caller_v1.gpu_caller_step(text,text)": "0369_inference_gpu.sql",
    "yoao_inference_gpu_collector_v1.gpu_collector_step(text,text)": "0395_plugin_gpu_retirement.sql",
    "yoao_labeling_v1.screenshot_operations_command(jsonb)": "0432_labeling_screenshot_operations.sql",
    "yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])": "0364_labeling_scale_seed_operations.sql",
    "yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)": "0364_labeling_scale_seed_operations.sql",
    "yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)": "0364_labeling_scale_seed_operations.sql",
    "yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)": "0364_labeling_scale_seed_operations.sql",
    "yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])": "0361_labeling_compatibility_reads.sql",
    "yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])": "0371_labeling_core_main_integration.sql",
    "yoao_labeling_v1.next_batch_display_id()": "0361_labeling_compatibility_reads.sql",
    "yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)": "0352_labeling_core_read.sql",
    "yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)": "0371_labeling_core_main_integration.sql",
    "yoao_inference_v1.ai_mask_pool_reference_row(uuid)": "0356_inference_pool_reference.sql",
    "yoao_labeling_v1.video_control_command(jsonb)": "0350_labeling_video_control.sql",
    "yoao_labeling_v1.video_segment_lifecycle_command(jsonb)": "0360_labeling_video_segment_lifecycle.sql",
    "yoao_labeling_v1.annotation_transfer_command(jsonb)": "0431_labeling_mask_staging.sql",
    "yoao_labeling_v1._annotation_transfer_exact_fields(jsonb,text[])": "0351_labeling_annotation_transfer.sql",
    "yoao_labeling_v1._annotation_transfer_without_track_ids(jsonb)": "0351_labeling_annotation_transfer.sql",
    "yoao_labeling_v1._annotation_transfer_without_suffix(text)": "0351_labeling_annotation_transfer.sql",
    "yoao_labeling_v1._annotation_transfer_camera_bbox_valid(jsonb)": "0351_labeling_annotation_transfer.sql",
    "yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)": "0371_labeling_core_main_integration.sql",
    "yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)": "0396_labeling_original_relations.sql",
    "yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)": "0352_labeling_core_read.sql",
    "yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)": "0352_labeling_core_read.sql",
    "yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer)": "0414_export_core_locks.sql",
    "yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer)": "0414_export_core_locks.sql",
    "yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer)": "0414_export_core_locks.sql",
    "yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer)": "0414_export_core_locks.sql",
    "yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer)": "0428_video_export_core_reads.sql",
    "yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer)": "0428_video_export_core_reads.sql",
    "yoao_labeling_v1.mask_qc_command(jsonb)": "0353_labeling_mask_quality.sql",
    "yoao_labeling_v1.mask_repair_command(jsonb)": "0385_labeling_repair_rollback_shard.sql",
    "yoao_labeling_v1.raster_mask_retention_query(jsonb)": "0353_labeling_mask_quality.sql",
    "yoao_labeling_v1._quality_exact_keys(jsonb,text[])": "0353_labeling_mask_quality.sql",
    "yoao_labeling_v1._raster_mask_reference_keys()": "0353_labeling_mask_quality.sql",
    "yoao_labeling_v1.spatial_quality_command(jsonb)": "0354_labeling_spatial_quality.sql",
    "yoao_labeling_v1.task_maintenance_command(jsonb)": "0383_labeling_task_delete_retention.sql",
    "yoao_labeling_v1.discussion_command(jsonb)": "0347_labeling_discussion.sql",
    "yoao_labeling_v1.task_event_command(jsonb)": "0347_labeling_discussion.sql",
    "yoao_labeling_v1.mask_edit_command(jsonb)": "0348_labeling_mask_edits.sql",
    "yoao_labeling_v1.mask_revision_command(jsonb)": "0422_mask_revision_digest.sql",
    "yoao_labeling_v1._mask_revision_canonical_json(jsonb)": "0422_mask_revision_digest.sql",
    "yoao_labeling_v1.ai_mask_accept_command(jsonb)": "0406_labeling_mask_decision_lock.sql",
    "yoao_labeling_v1.scene_read_query(jsonb)": "0349_labeling_scene_read.sql",
    "yoao_labeling_v1._scene_read_track_json(uuid,uuid,uuid,text,text,text,jsonb,jsonb,integer,timestamptz)": "0349_labeling_scene_read.sql",
    "yoao_labeling_v1._scene_read_interval_json(uuid,uuid,integer,integer,text,integer)": "0349_labeling_scene_read.sql",
    "yoao_labeling_v1._scene_read_annotation_json(uuid,uuid,uuid,text,text,text,jsonb,text,uuid,text,text,boolean,boolean,boolean,boolean,integer,timestamptz,integer)": "0349_labeling_scene_read.sql",
    "yoao_labeling_v1.prediction_record_command(jsonb)": "0372_prediction_failure_snapshot.sql",
    "yoao_labeling_v1.prediction_partition_command(jsonb)": "0357_labeling_prediction_partitions.sql",
    "yoao_labeling_v1.scene_track_command(jsonb)": "0343_labeling_scene_track_commands.sql",
    "yoao_labeling_v1.cross_frame_write_command(jsonb)": "0344_labeling_cross_frame_writes.sql",
    "yoao_labeling_v1.annotation_prediction_command(jsonb)": "0345_labeling_annotation_prediction.sql",
    "yoao_labeling_v1.annotation_maintenance_command(jsonb)": "0346_labeling_annotation_maintenance.sql",
    "yoao_labeling_v1.annotation_bulk_command(jsonb)": "0387_labeling_annotation_lineage.sql",
    "yoao_labeling_v1.video_bbox_editing_command(jsonb)": "0340_labeling_video_bbox_editing.sql",
    "yoao_labeling_v1.batch_control_command(jsonb)": "0341_labeling_batch_control.sql",
    "yoao_labeling_v1.annotation_statistics_command(jsonb)": "0336_labeling_annotation_statistics.sql",
    "yoao_labeling_v1.scene_track_domain_command(jsonb)": "0337_labeling_scene_track_domain.sql",
    "yoao_labeling_v1.annotation_write_command(jsonb)": "0338_labeling_annotation_writes.sql",
    "public.capture_mask_annotation_revision()": "0338_labeling_annotation_revision_support.sql",
    "public.record_mask_annotation_revision(uuid,uuid,uuid,integer,jsonb,text)": "0338_labeling_annotation_revision_support.sql",
    "yoao_labeling_v1.task_edit_lock_command(jsonb)": "0466_task_edit_lock_domain.sql",
    "yoao_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)": "0466_task_edit_lock_domain.sql",
    "yoao_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])": "0466_task_edit_lock_domain.sql",
    "yoao_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)": "0466_task_edit_lock_domain.sql",
    "yoao_labeling_v1.task_workflow_command(jsonb)": "0427_labeling_actor_batch_share.sql",
    "yoao_jobs_v1.lock_action_idempotency(text,uuid,uuid,text)": "0332_jobs_action_idempotency.sql",
    "yoao_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text)": "0332_jobs_action_idempotency.sql",
    "yoao_jobs_v1.find_video_retry_job(uuid,uuid,text)": "0407_jobs_video_retry.sql",
    "yoao_jobs_v1.lock_video_retry_jobs(uuid)": "0407_jobs_video_retry.sql",
    "yoao_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid)": "0432_jobs_fixture_presentation.sql",
    "yoao_jobs_v1.delete_fixture_project_jobs(uuid[])": "0436_fixture_jobs_join.sql",
    "yoao_jobs_v1.delete_fixture_media_backfills(uuid,uuid)": "0446_fixture_media_backfill_cleanup.sql",
    "yoao_jobs_v1.delete_fixture_presentation_jobs(uuid,uuid,uuid[])": "0432_jobs_fixture_guard.sql",
    "yoao_labeling_v1.guard_fixture_presentation_failures(uuid,uuid,uuid[])": "0432_labeling_fixture_retry_guard.sql",
    "yoao_labeling_v1.guard_fixture_project_retry_anchors(uuid[])": "0432_labeling_fixture_retry_guard.sql",
    "yoao_labeling_v1.delete_fixture_project_rows(uuid[])": "0358_labeling_fixture_cleanup.sql",
    **{
        signature: "0329_jobs_domain_queries.sql"
        for signature in JOBS_DOMAIN_QUERY_FUNCTIONS
    },
    "yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid)": "0389_learning_deployment_retirement.sql",
    **{signature: "0328_jobs_row_locks.sql" for signature in JOBS_ROW_LOCK_FUNCTIONS},
    "yoao_jobs_v1.read_job_for_update_nowait(uuid)": "0415_export_job_nowait.sql",
    **{
        signature: "0327_jobs_lifecycle.sql"
        for signature in (
            *JOBS_LIFECYCLE_FUNCTIONS,
            *(
                "yoao_jobs_v1._jobs_lifecycle_has_keys(jsonb,text[])",
                "yoao_jobs_v1._jobs_lifecycle_is_truthy(jsonb)",
                "yoao_jobs_v1._jobs_lifecycle_integer(jsonb)",
            ),
        )
    },
    **{
        signature: "0326_jobs_list_query.sql"
        for signature in (
            *JOBS_LIST_QUERY_FUNCTIONS,
            "yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)",
        )
    },
    **{
        signature: "0325_notifications_delivery_read.sql"
        for signature in NOTIFICATION_DELIVERY_READ_FUNCTIONS
    },
    "yoao_audit_v1.refresh_audit_bi_mv()": "0322_audit_daily_projection.sql",
    "yoao_audit_v1.maintenance_command(jsonb)": "0438_fixture_audit_namespace.sql",
    "yoao_audit_v1.platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz)": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1._assert_platform_partition(date,bigint,bigint)": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1.archive_output_has_ready(integer,integer)": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1.archive_output_ready_month(integer,integer)": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1.archive_output_ready_list()": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1.archive_output_pending(uuid)": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1.archive_output_history()": "0485_audit_platform_maintenance.sql",
    "yoao_audit_v1.archive_output_save_candidate(uuid,uuid,date,bigint,bigint,bigint,bigint,text,bigint,text,jsonb,timestamptz)": "0485_audit_platform_maintenance.sql",
    "yoao_notifications_v1.append_job_terminal_notification(jsonb)": "0484_notifications_terminal_outbox.sql",
    "yoao_notifications_v1.claim_identity_deliveries(jsonb)": "0484_notifications_terminal_outbox.sql",
    "yoao_audit_v1.query_events(pg_catalog.jsonb)": "0317_audit_query.sql",
    **{
        signature: "0324_notifications_inbox_query.sql"
        for signature in (
            *NOTIFICATION_INBOX_FUNCTIONS,
            NOTIFICATION_INBOX_PRIVATE_HELPER,
        )
    },
    "yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)": "0373_prediction_execution_snapshot_binding.sql",
    **{
        signature: "0371_notifications_main_integration.sql"
        for signature in (
            "yoao_notifications_v1._notification_is_migration_inventory(uuid)",
            "yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text)",
            "yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer)",
            "yoao_notifications_v1.inbox_total_count_scoped(uuid,text)",
            "yoao_notifications_v1.inbox_unread_count_scoped(uuid,text)",
        )
    },
    **{
        signature: "0421_notification_job_scope.sql"
        for signature in (
            "yoao_notifications_v1._notification_unknown_job_scope(uuid)",
            "yoao_notifications_v1.read_state_command(jsonb)",
            "yoao_notifications_v1.read_state_command_scoped(jsonb)",
            "yoao_notifications_v1.delete_command(pg_catalog.jsonb)",
            "yoao_notifications_v1.delete_command_scoped(jsonb)",
        )
    },
    NOTIFICATION_INBOX_PRIVATE_HELPER: "0475_inbox_retained_job_scope.sql",
    **{
        signature: "0374_workspace_guide_publication.sql"
        for signature in (
            "yoao_workspace_configuration_v1._guide_entry_matches(jsonb,uuid,uuid,text,text,integer,timestamptz)",
            "yoao_workspace_configuration_v1._guide_lock_project(uuid,text)",
            "yoao_workspace_configuration_v1._guide_authority(uuid,uuid)",
            "yoao_workspace_configuration_v1._guide_storage_gate(uuid,bigint)",
            "yoao_workspace_configuration_v1._guide_failure(jsonb,text,integer,text)",
            "yoao_workspace_configuration_v1.guide_assets_step(jsonb,text,jsonb)",
            "yoao_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])",
        )
    },
    **{
        signature: "0375_original_authority_receipts.sql"
        for signature in (
            "yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid)",
            "yoao_jobs_v1.read_job_execution_snapshot(uuid)",
            "yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)",
            "yoao_assets_v1.read_task_original_binding(uuid)",
            "yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)",
            "yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)",
            "yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)",
            "yoao_labeling_v1.publish_task_original(uuid,uuid,uuid,uuid,uuid,text)",
        )
    },
    **{
        signature: "0376_assets_connection_runtime.sql"
        for signature in (
            "yoao_assets_v1._connection_probe_reply(jsonb,text)",
            "yoao_assets_v1._connection_authority_proof(uuid,uuid,text[],jsonb)",
            "yoao_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[])",
            "yoao_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean)",
            "yoao_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb)",
        )
    },
    "yoao_identity_accounts_v1._lock_current_actor(jsonb,text,text)": "0423_identity_presence_actor.sql",
    **{
        signature: "0423_identity_presence.sql"
        for signature in (
            "yoao_identity_v1._initialize_account_presence()",
            "yoao_identity_sessions_v1.execute_session_command(jsonb)",
            "yoao_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)",
            "yoao_identity_recovery_v1.apply_recovery_command(jsonb)",
            "yoao_identity_deactivation_v1.process_due_deactivations(jsonb)",
            "yoao_identity_directory_v1.execute_directory_command(jsonb)",
            "yoao_identity_suspensions_v1.execute_suspension_command(jsonb)",
            "yoao_identity_deletions_v1.execute_deletion_command(jsonb)",
            "yoao_identity_offboarding_v1.apply_offboarding_command(jsonb)",
            "yoao_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)",
        )
    },
    "yoao_identity_invitations_v1.apply_invitation_command(jsonb)": "0432_identity_invitation_command.sql",
    **{
        signature: "0432_identity_fixture_invitations.sql"
        for signature in (
            "yoao_identity_invitations_v1.bind_fixture_invitation()",
            "yoao_identity_invitations_v1.bind_fixture_invitation_birth(uuid,uuid)",
            "yoao_identity_invitations_v1.lock_fixture_invitation_run(uuid)",
            "yoao_identity_invitations_v1.guard_fixture_invitation_acceptance()",
        )
    },
    **{
        signature: "0425_identity_preferences.sql"
        for signature in (
            "yoao_identity_v1._initialize_account_preferences()",
            "yoao_identity_accounts_v1._account_record(uuid)",
            "yoao_identity_accounts_v1.prepare_account_command(jsonb)",
            "yoao_identity_accounts_v1.apply_account_command(jsonb)",
            "yoao_identity_groups_v1._account_record(uuid)",
            "yoao_identity_deactivation_v1._account_record(uuid)",
            "yoao_identity_invitations_v1._account_record(uuid)",
            "yoao_identity_directory_v1._directory_user(uuid)",
            "yoao_identity_fixture_v1.create_account(uuid,text,text,text,text)",
            "yoao_identity_v1.replace_fixture_preferences(uuid,jsonb)",
        )
    },
    **{
        signature: "0425_identity_provisioning.sql"
        for signature in (
            "yoao_identity_v1.create_provisioning_account(jsonb)",
            "yoao_identity_v1.create_seed_persona_account(jsonb)",
        )
    },
    **{
        signature: "0426_image_admission_share.sql"
        for signature in (
            "yoao_labeling_v1.read_image_task_source_share(uuid,uuid,boolean)",
            "yoao_assets_v1._image_labeling_task_share(uuid,uuid,boolean)",
            "yoao_assets_v1._image_owner_source_share(uuid,uuid,uuid,boolean)",
            "yoao_assets_v1._image_accept_source(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,text)",
        )
    },
    **{
        signature: "0377_workspace_project_snapshots.sql"
        for signature in (
            "yoao_workspace_project_query_v1._project_row_json(jsonb,integer,integer,integer,integer,text)",
            "yoao_workspace_configuration_v1._class_rename_project_row(uuid,text)",
            "yoao_workspace_configuration_v1._guide_asset_read_project_row(uuid)",
            "yoao_workspace_project_query_v1._project_query_step_impl(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamp with time zone,timestamp with time zone,bigint,integer)",
        )
    },
    "yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,uuid)": "0499_labeling_artifact_boundaries.sql",
    "yoao_jobs_v1.read_job_progress(uuid)": "0382_jobs_row_progress_facts.sql",
    "yoao_jobs_v1.read_job_progress_for_update(uuid,text,text)": "0384_jobs_row_progress_write.sql",
    "yoao_jobs_v1.record_job_progress(uuid,text,text,timestamptz,jsonb)": "0384_jobs_row_progress_write.sql",
    **{
        signature: "0379_operator_item_locator.sql"
        for signature in (
            "yoao_assets_v1.publish_operator_item_original(jsonb)",
            "yoao_assets_v1.operator_item_original_facts(uuid,uuid)",
            "yoao_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid)",
        )
    },
    **{
        signature: "0379_operator_auxiliary.sql"
        for signature in (
            "yoao_assets_v1._managed_original_import_prefix(text)",
            "yoao_assets_v1._managed_original_scope_key(text,text)",
            "yoao_assets_v1._operator_read_permit_purpose(uuid)",
            "yoao_assets_v1.publish_operator_auxiliary(uuid)",
            "yoao_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid)",
            "yoao_assets_v1.operator_auxiliary_material(uuid,uuid)",
            "yoao_assets_v1.operator_item_original_material(uuid,uuid)",
            "yoao_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid)",
            "yoao_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid)",
            "yoao_assets_v1.operator_dataset_state(uuid)",
            "yoao_assets_v1.operator_scene_state(uuid,uuid)",
            "yoao_assets_v1.open_operator_storage_operation(uuid)",
            "yoao_assets_v1.record_operator_original_prepared(uuid,jsonb)",
            "yoao_assets_v1.mark_operator_original_writing(uuid)",
            "yoao_assets_v1.record_operator_original_written(uuid,jsonb)",
            "yoao_assets_v1.record_operator_original_verified(uuid,jsonb)",
            "yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)",
        )
    },
    **{
        signature: "0459_source_ingest_identity_and_retry.sql"
        for signature in (
            "yoao_assets_v1.begin_operator_auxiliary(jsonb)",
            "yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb)",
            "yoao_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
        )
    },
    "yoao_assets_v1.classify_fixture_original_reclaim(uuid,text)": "0432_assets_fixture_reclaim_guard.sql",
    "yoao_assets_v1.is_fixture_original_source(text,text)": "0432_assets_fixture_source_guard.sql",
    "yoao_assets_v1.guard_fixture_operator_source_receipt()": "0432_assets_fixture_source_guard.sql",
    "yoao_assets_v1.require_fixture_operator_manifest()": "0432_assets_fixture_auxiliary.sql",
    "yoao_assets_v1.guard_fixture_auxiliary_intent()": "0432_assets_fixture_auxiliary.sql",
    "yoao_fixture_v1.retire_auxiliary_original(uuid,uuid,uuid)": "0440_fixture_auxiliary_retirement.sql",
    "yoao_fixture_v1.guard_auxiliary_retirement()": "0440_fixture_auxiliary_retirement.sql",
    "yoao_assets_v1.guard_scale_fixture_originals(uuid,uuid[])": "0432_assets_scale_fixture_guard.sql",
    "yoao_labeling_v1.lock_task_original_pointer(uuid,text)": "0432_labeling_original_read.sql",
    "yoao_jobs_v1.guard_fixture_project_jobs(uuid[])": "0436_fixture_jobs_join.sql",
    "yoao_jobs_v1.guard_fixture_dataset_jobs(uuid[])": "0465_worker_job_dataset_scope.sql",
    "yoao_jobs_v1.fixture_dataset_job_scope(uuid,text,jsonb,uuid,uuid)": "0437_fixture_reclaim_job_scope.sql",
    "yoao_jobs_v1.fixture_job_dataset_scope(uuid,text,jsonb,uuid,uuid)": "0465_worker_job_dataset_scope.sql",
    **{
        signature: "0465_worker_job_dataset_scope.sql"
        for signature in (
            "yoao_jobs_v1.worker_job_scope_digest(text,jsonb)",
            "yoao_jobs_v1.guard_worker_job_dataset_receipt()",
            "yoao_jobs_v1.record_worker_job_dataset_scope()",
            "yoao_jobs_v1.guard_worker_job_scope_change()",
            "yoao_jobs_v1.cross_frame_job_birth(uuid,uuid,uuid,jsonb)",
            "yoao_jobs_v1.cross_frame_birth_identity_matches(uuid,uuid,uuid,jsonb)",
            "yoao_jobs_v1.retain_unsettled_worker_deletion()",
        )
    },
    **{
        signature: "0465_cross_frame_born_dataset_scope.sql"
        for signature in (
            "yoao_assets_v1._cross_frame_scope_uuid(jsonb)",
            "yoao_assets_v1._cross_frame_scope_int(jsonb)",
            "yoao_assets_v1._cross_frame_dataset_scope(uuid,uuid,jsonb)",
            "yoao_assets_v1.cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb)",
            "yoao_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb)",
        )
    },
    **{
        signature: "0465_retry_born_dataset_scope.sql"
        for signature in (
            "yoao_jobs_v1.bind_prediction_retry_attempt_birth()",
            "yoao_jobs_v1.guard_prediction_retry_attempt_scope()",
            "yoao_jobs_v1.guard_retry_born_authority()",
            "yoao_jobs_v1.guard_retry_born_dataset_scope()",
            "yoao_jobs_v1.retry_authority_snapshot_matches(uuid,jsonb,uuid)",
            "yoao_jobs_v1.retry_born_dataset_scope(uuid,uuid,uuid,jsonb)",
            "yoao_assets_v1.retry_task_dataset_scope(uuid,uuid,uuid,text,text,uuid,jsonb)",
            "yoao_assets_v1.retry_authority_binding_matches(uuid,uuid,uuid,text,text,text,text,jsonb)",
        )
    },
    "yoao_jobs_v1.record_fixture_prediction_job_birth()": "0456_prediction_dataset_scope.sql",
    "yoao_jobs_v1.guard_fixture_prediction_job_datasets()": "0456_prediction_dataset_scope.sql",
    "yoao_assets_v1.fixture_task_dataset_scope(uuid,jsonb)": "0456_prediction_dataset_scope.sql",
    "yoao_jobs_v1.create_filter_fixture_jobs(uuid,uuid,uuid,uuid,uuid,uuid,text,text)": "0432_jobs_fixture_presentation.sql",
    "yoao_fixture_v1.guard_jobs_anchor()": "0436_fixture_jobs_join.sql",
    "yoao_fixture_v1.guard_dataset_job_anchor()": "0446_fixture_media_dataset_guards.sql",
    **{
        signature: "0444_image_export_scope.sql"
        for signature in (
            "public.image_export_snapshot_datasets(jsonb)",
            "public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[])",
            "public.image_export_scope_matches(uuid,uuid)",
            "public.guard_image_export_scope()",
            "public.guard_image_export_scope_transition()",
            "public.guard_image_export_job_birth()",
            "public.guard_image_export_fixture_dataset_birth()",
            "public.check_image_export_scope_committed()",
        )
    },
    **{
        signature: "0432_jobs_fixture_presentation.sql"
        for signature in (
            "yoao_fixture_v1.guard_presentation_job_update()",
            "yoao_fixture_v1.guard_presentation_job_dispatch()",
        )
    },
    **{
        signature: "0432_fixture_dataset_identity.sql"
        for signature in (
            "yoao_fixture_v1.guard_original_target()",
            "yoao_fixture_v1.validate_run_original_target()",
            "yoao_fixture_v1.guard_dataset_storage_identity()",
            "yoao_fixture_v1.guard_run_original_store()",
            "yoao_fixture_v1.guard_maintenance_window()",
        )
    },
    **{
        signature: "0432_labeling_screenshot_operations.sql"
        for signature in (
            "yoao_labeling_v1._fixture_screenshot_project(uuid)",
            "yoao_labeling_v1._fixture_screenshot_path(uuid,uuid)",
            "yoao_labeling_v1._fixture_screenshot_task(uuid,uuid)",
            "yoao_labeling_v1._fixture_screenshot_user(uuid,text,text)",
        )
    },
    **{
        signature: "0513_inference_plugin_direct_mode.sql"
        for signature in (
            "yoao_inference_routing_v1.prediction_scope_reserve(text)",
            "yoao_inference_routing_v1.prediction_test_scope_reserve(text)",
        )
    },
    **{
        signature: "0434_prediction_execution_routing.sql"
        for signature in (
            "yoao_inference_routing_v1.guard_prediction_scope_insert()",
            "yoao_inference_routing_v1.guard_prediction_dispatch_start()",
            "yoao_inference_routing_v1.guard_prediction_managed_attempt()",
            "yoao_inference_routing_v1.fixture_prediction_scope_inventory(uuid,uuid[],uuid[])",
            "yoao_inference_routing_v1.fixture_prediction_tests_retired(uuid,uuid[],uuid[])",
        )
    },
    **{
        signature: "0434_prediction_output_ownership.sql"
        for signature in (
            "yoao_jobs_v1.begin_prediction_mask_producer(jsonb,bigint,uuid)",
            "yoao_jobs_v1.bind_prediction_mask_object(uuid,uuid)",
            "yoao_jobs_v1.finish_prediction_mask_producer(uuid)",
            "yoao_jobs_v1.prediction_mask_object_retained(uuid)",
            "yoao_jobs_v1.prediction_fixture_open(uuid)",
            "yoao_jobs_v1.preserve_fixture_prediction_dispatches()",
            "yoao_jobs_v1.fixture_prediction_retired(uuid,uuid,uuid)",
            "yoao_jobs_v1.guard_fixture_prediction_ledgers(uuid,uuid)",
            "yoao_assets_v1.fixture_prediction_inputs_retired(uuid,uuid[])",
        )
    },
    "public.guard_fixture_export_retention()": "0435_fixture_export_retention.sql",
    **{
        signature: "0435_fixture_export_zip_retirement.sql"
        for signature in (
            "public.guard_fixture_export_zip_state()",
            "public.record_fixture_export_zip_creation()",
            "public.guard_fixture_export_zip_receipt()",
        )
    },
    "public.fixture_export_session_owns_job(uuid)": "0435_fixture_export_execution.sql",
    **{
        signature: "0447_export_fixture_rebind.sql"
        for signature in (
            "public.guard_fixture_export_execution()",
            "public.record_fixture_export_execution()",
            "public.guard_fixture_export_job_identity()",
            "public.retire_fixture_export_execution(uuid,uuid,uuid)",
            "public.fixture_export_execution_retired(uuid,uuid,uuid)",
            "public.capture_fixture_native_export()",
            "public.capture_fixture_native_export_output()",
            "public.fixture_native_export_cleanup_eligible(uuid)",
        )
    },
    **{
        signature: "0435_fixture_native_export.sql"
        for signature in (
            "public.guard_fixture_native_export_evidence()",
            "public.fixture_native_export_lifetime(uuid,uuid)",
            "public.fixture_export_retired(uuid,uuid,uuid)",
        )
    },
    "public.fixture_export_owner_session()": "0435_fixture_export_owner.sql",
    "public.guard_fixture_export_private_retirement()": "0435_fixture_export_owner.sql",
    "public.retire_fixture_native_export_output(uuid)": "0435_fixture_export_owner.sql",
    **{
        signature: "0442_fixture_export_zip_versions.sql"
        for signature in (
            "public.guard_export_zip_version_proof()",
            "public.capture_fixture_export_zip_version()",
            "public.guard_fixture_export_zip_version()",
            "public.confirm_fixture_export_zip_absent(uuid)",
            "public.guard_fixture_zip_retirement_proof()",
        )
    },
    **{
        signature: "0447_export_execution.sql"
        for signature in (
            "yoao_export_v1.publications_retired(uuid)",
            "yoao_export_v1.capture_execution_birth()",
            "yoao_export_v1.capture_execution_delivery()",
            "yoao_export_v1.session_holds_lock(text,uuid,integer)",
            "yoao_export_v1.begin_execution(uuid,uuid,text,uuid,integer)",
            "yoao_export_v1.enter_execution_io(uuid,uuid)",
            "yoao_export_v1.quiesce_execution_io(uuid,uuid)",
            "yoao_export_v1.finish_execution(uuid,uuid)",
            "yoao_export_v1.retire_execution(uuid,uuid)",
        )
    },
    **{
        signature: "0447_jobs_publication_execution.sql"
        for signature in (
            "yoao_jobs_v1.guard_publication_attempt()",
            "yoao_jobs_v1.guard_publication_retirement()",
            "yoao_jobs_v1.guard_publication_birth()",
            "yoao_jobs_v1.capture_publication_job_birth()",
            "yoao_jobs_v1.capture_publication_intent_birth()",
            "yoao_jobs_v1.job_publications_retired(uuid,text[])",
            "yoao_jobs_v1.job_publications_not_sent(uuid,text[])",
            "yoao_jobs_v1.publication_session_owns(uuid)",
            "yoao_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid)",
            "yoao_jobs_v1.finish_dispatch_publication(uuid,uuid,text,bigint,uuid)",
            "yoao_jobs_v1.refuse_dispatch_publication(uuid,uuid,text,bigint,uuid)",
            "yoao_jobs_v1.retry_publication_intent(uuid,text,text,jsonb,jsonb)",
            "yoao_jobs_v1.begin_retry_publication(uuid,uuid,text,text,jsonb,jsonb,integer,uuid)",
            "yoao_jobs_v1.finish_retry_publication(uuid,uuid,text,integer,uuid)",
            "yoao_jobs_v1.guard_retired_dispatch()",
        )
    },
    **{
        signature: "0447_jobs_fixture_media_publication.sql"
        for signature in (
            "yoao_jobs_v1.guard_fixture_media_publication_record()",
            "yoao_jobs_v1.guard_fixture_media_publication_attempt()",
            "yoao_jobs_v1.lock_fixture_media_run(uuid,boolean)",
            "yoao_jobs_v1.fixture_media_publisher_owns(uuid)",
            "yoao_jobs_v1.prepare_fixture_media_delivery(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb)",
            "yoao_jobs_v1.fixture_media_delivery(uuid)",
            "yoao_jobs_v1.fixture_media_publication_accepted(uuid,uuid,text)",
            "yoao_jobs_v1.begin_fixture_media_publication(uuid,uuid)",
            "yoao_jobs_v1.finish_fixture_media_publication(uuid,uuid,text)",
            "yoao_jobs_v1.retire_fixture_media_publication(uuid,uuid,text)",
            "yoao_jobs_v1.fixture_media_publication_retired(uuid,uuid,text)",
            "yoao_jobs_v1.fixture_media_publication_retirement_outcome(uuid,uuid,text)",
            "yoao_jobs_v1.prepare_media_delivery(jsonb)",
        )
    },
    "yoao_jobs_v1.observe_fixture_annotation_import()": "0480_fixture_annotation_import.sql",
    "yoao_assets_v1.prepare_fixture_video_request_delivery()": "0481_fixture_video_requests.sql",
    "yoao_jobs_v1.fixture_real_job_retired(uuid,uuid,uuid)": "0483_fixture_prediction_import.sql",
    "yoao_jobs_v1.observe_fixture_prediction_import()": "0483_fixture_prediction_import.sql",
    "yoao_jobs_v1.preserve_fixture_export_dispatch()": "0436_fixture_jobs_join.sql",
    **{
        signature: "0432_assets_media_source_read.sql"
        for signature in (
            "yoao_assets_v1._managed_reclaim_json_string(text)",
            "yoao_assets_v1._managed_reclaim_lock_identity(text,text)",
        )
    },
    **{
        signature: "0432_assets_media_source_admission.sql"
        for signature in ("yoao_assets_v1._begin_media_original_read(uuid,uuid,text)",)
    },
    "yoao_assets_v1._finish_media_original_read(uuid,uuid)": "0445_assets_fixture_reader_completion.sql",
    "yoao_assets_v1.fixture_original_readers_empty(uuid)": "0446_fixture_original_readers.sql",
    **{
        signature: "0432_assets_spatial_original_admission.sql"
        for signature in (
            "yoao_assets_v1.is_fixture_spatial_dataset(uuid)",
            "yoao_assets_v1.begin_spatial_calibration_original(uuid,uuid,text)",
            "yoao_assets_v1.finish_spatial_calibration_original(uuid,uuid)",
        )
    },
    **{
        signature: "0432_assets_media_source_authority.sql"
        for signature in (
            "yoao_assets_v1.begin_media_original_actor(uuid,uuid,uuid,text,text)",
            "yoao_assets_v1.begin_media_original_worker(uuid,bigint,uuid,text,text)",
            "yoao_assets_v1.begin_media_original_system(uuid,text,text)",
            "yoao_assets_v1.finish_media_original_read(uuid,uuid)",
            "yoao_assets_v1.finish_media_original_worker(uuid,uuid)",
            "yoao_assets_v1.finish_media_original_system(uuid,uuid)",
        )
    },
    **{
        signature: "0432_assets_images_source_authority.sql"
        for signature in (
            "yoao_assets_v1.begin_image_original_actor(uuid,uuid,uuid,text,text)",
            "yoao_assets_v1.begin_image_original_worker(uuid,bigint,uuid,text,text)",
            "yoao_assets_v1.begin_image_original_system(text,uuid,text,text)",
            "yoao_assets_v1.begin_image_original_backfill(uuid,bigint,uuid,uuid,text,uuid,text,text)",
            "yoao_assets_v1.finish_image_original_read(uuid,uuid)",
            "yoao_assets_v1.finish_image_original_worker(uuid,uuid)",
            "yoao_assets_v1.finish_image_original_system(uuid,uuid)",
        )
    },
    **{
        signature: "0432_assets_inputs_source_authority.sql"
        for signature in (
            "yoao_assets_v1.begin_accepted_image_original(uuid,bigint,uuid)",
            "yoao_assets_v1.finish_accepted_image_original(uuid,uuid)",
        )
    },
    **{
        signature: "0432_assets_video_inputs_source_authority.sql"
        for signature in (
            "yoao_assets_v1._begin_video_input_original(uuid,bigint,uuid,text)",
            "yoao_assets_v1.begin_accepted_video_original(uuid,bigint,uuid)",
            "yoao_assets_v1.begin_tracker_video_original(uuid,bigint,uuid)",
            "yoao_assets_v1.finish_accepted_video_original(uuid,uuid)",
            "yoao_assets_v1.finish_tracker_video_original(uuid,uuid)",
        )
    },
    **{
        signature: "0432_assets_video_inputs_control_source.sql"
        for signature in (
            "yoao_assets_v1.begin_video_member_original(uuid,uuid,uuid,text,bigint,uuid,text,text)",
            "yoao_assets_v1.begin_video_control_original(uuid,uuid,uuid,uuid,text,text,text)",
            "yoao_assets_v1.finish_video_control_original(uuid,uuid)",
        )
    },
    **{
        signature: "0432_assets_tracker_control_source.sql"
        for signature in (
            "yoao_assets_v1.begin_tracker_prepare_original(uuid,uuid,uuid,uuid,text,text,text)",
            "yoao_assets_v1.begin_tracker_control_original(uuid,uuid,uuid,uuid,text,text,text,text)",
            "yoao_assets_v1.finish_tracker_control_original(uuid,uuid)",
        )
    },
    **{
        signature: "0446_fixture_native_media_sources.sql"
        for signature in (
            "yoao_assets_v1.begin_image_original_worker(uuid,bigint,uuid,text,text)",
            "yoao_assets_v1.begin_media_original_worker(uuid,bigint,uuid,text,text)",
        )
    },
    "yoao_assets_v1.begin_image_original_backfill(uuid,bigint,uuid,uuid,text,uuid,text,text)": "0446_fixture_native_media_backfill_source.sql",
    **{
        signature: "0446_fixture_native_media_scans.sql"
        for signature in (
            "yoao_assets_v1.scan_image_backfill(jsonb,text,uuid,integer)",
            "yoao_assets_v1.scan_video_backfill(jsonb,uuid,integer)",
            "yoao_assets_v1._media_backfill_snapshot(jsonb,timestamptz)",
            "yoao_assets_v1.advance_video_backfill(uuid,bigint,uuid,integer)",
            "yoao_labeling_v1.scan_image_task_sources(jsonb,uuid,integer)",
        )
    },
    **{
        signature: "0446_fixture_native_media_claims.sql"
        for signature in (
            "yoao_assets_v1.claim_image_media(uuid,uuid,integer,text[])",
            "yoao_assets_v1.claim_video_media(uuid,uuid,integer,text[])",
        )
    },
    **{
        signature: "0446_fixture_native_media_garbage.sql"
        for signature in (
            "yoao_assets_v1.claim_image_garbage(uuid,integer,integer)",
            "yoao_assets_v1.claim_video_media_garbage(uuid,integer,integer)",
        )
    },
    **{
        signature: "0448_fixture_external_pyramid_reuse.sql"
        for signature in (
            "yoao_assets_v1.record_fixture_external_pyramid_reuse(text,uuid,uuid,uuid,integer)",
            "yoao_assets_v1.fixture_external_image_attempt_sources_proven(uuid,uuid,uuid,uuid,bigint,uuid)",
            "yoao_assets_v1.fixture_external_image_attempt_sources_settled(uuid,uuid,uuid,uuid,bigint,uuid)",
            "yoao_jobs_v1.bind_fixture_media_child_generation(uuid,uuid,uuid,integer)",
            "yoao_assets_v1.bind_fixture_independent_pyramid(uuid,uuid,uuid,integer)",
            "yoao_assets_v1.fixture_dataset_media_producers_quiescent(uuid,uuid)",
        )
    },
    **{
        signature: "0455_images_output_ownership.sql"
        for signature in (
            "yoao_assets_v1._accept_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb,text)",
            "yoao_assets_v1.prepare_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb)",
            "yoao_assets_v1.retry_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb)",
            "yoao_assets_v1.guard_image_pyramid_native_attempt()",
            "yoao_assets_v1.guard_image_pyramid_native_member()",
            "guard_image_pyramid_inputs()",
            "yoao_assets_v1._image_garbage_prefix_valid(text)",
            "yoao_assets_v1._native_pyramid_attempt(uuid,boolean)",
            "yoao_assets_v1.begin_native_pyramid_member(uuid,uuid,text,text,bigint)",
            "yoao_assets_v1.submit_native_pyramid_member(uuid)",
            "yoao_assets_v1._native_pyramid_fixture_scope(uuid)",
            "yoao_assets_v1.finish_native_pyramid_output(uuid,bigint,uuid,jsonb)",
            "yoao_assets_v1.record_fixture_media_birth()",
        )
    },
    "yoao_insights_v1.refresh_user_perf_daily()": "0487_insights_perf_refresh.sql",
    **{
        signature: "0487_insights_task_views.sql"
        for signature in (
            "yoao_insights_v1._task_view_project_scope(uuid,uuid,boolean)",
            "yoao_insights_v1._task_view_snapshot(public.project_task_views)",
            "yoao_insights_v1._lock_writable_task_view(uuid,uuid,uuid,boolean)",
            "yoao_insights_v1.list_saved_task_views(uuid,uuid,text)",
            "yoao_insights_v1.read_saved_task_view(uuid,uuid,uuid)",
            "yoao_insights_v1.create_saved_task_view(uuid,uuid,text,text,text,jsonb,jsonb,jsonb)",
            "yoao_insights_v1.update_saved_task_view(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb)",
            "yoao_insights_v1.delete_saved_task_view(uuid,uuid,uuid)",
        )
    },
    **{
        signature: "0487_insights_dataset_views.sql"
        for signature in (
            "yoao_insights_v1._dataset_view_snapshot(public.dataset_saved_views)",
            "yoao_insights_v1.list_personal_dataset_views(uuid,uuid)",
            "yoao_insights_v1.has_personal_dataset_view(uuid,uuid,uuid)",
            "yoao_insights_v1.prepare_personal_dataset_view(uuid,uuid)",
            "yoao_insights_v1.create_personal_dataset_view(uuid,uuid,text,jsonb)",
            "yoao_insights_v1.update_personal_dataset_view(uuid,uuid,uuid,text,jsonb)",
            "yoao_insights_v1.delete_personal_dataset_view(uuid,uuid,uuid)",
        )
    },
    **{
        signature: "0489_inference_prediction_runtime.sql"
        for signature in (
            "yoao_inference_topology_v1.lock_video_prediction_registry(uuid)",
            "yoao_inference_topology_v1.lock_video_prediction_pool(uuid,uuid,uuid)",
            "yoao_inference_topology_v1.lock_native_prediction_route(uuid,uuid,uuid)",
            "yoao_inference_pipelines_v1.create_prediction_execution_snapshot(uuid,text,jsonb)",
            "yoao_inference_pipelines_v1.read_prediction_execution_snapshot(uuid,uuid)",
            "yoao_inference_pipelines_v1.prune_prediction_execution_snapshots(timestamptz)",
            "yoao_jobs_v1.prediction_execution_snapshot_in_use(uuid)",
            "yoao_labeling_v1.prediction_execution_snapshot_in_use(uuid)",
        )
    },
    "yoao_labeling_v1.finish_prediction_retry_pipeline_failure(jsonb,jsonb)": (
        "0489_prediction_retry_pipeline_failure.sql"
    ),
    **{
        signature: "0489_prediction_input_leases.sql"
        for signature in (
            "yoao_assets_v1.create_prediction_input_lease(uuid,uuid,text,uuid,integer,jsonb,integer,jsonb,timestamptz)",
            "yoao_assets_v1.prediction_input_lease_facts(uuid,boolean,timestamptz)",
            "yoao_assets_v1.prediction_input_objects_pending(uuid)",
            "yoao_assets_v1.read_prediction_input_inventory(uuid)",
            "yoao_assets_v1.read_prediction_input_object(uuid,text)",
            "yoao_assets_v1.reserve_prediction_input_object(uuid,text,jsonb)",
            "yoao_assets_v1.verify_prediction_input_object(uuid,text,jsonb,text,text)",
            "yoao_assets_v1.seal_prediction_input_lease(uuid,boolean,timestamptz)",
            "yoao_assets_v1.prepare_prediction_input_cleanup(uuid,timestamptz)",
            "yoao_assets_v1.record_prediction_input_cleanup_failure(uuid,text,boolean)",
            "yoao_assets_v1.finish_prediction_input_cleanup(uuid,uuid,integer,timestamptz)",
            "yoao_assets_v1.list_due_prediction_input_cleanups(timestamptz,integer)",
            "yoao_assets_v1.prediction_input_binding_history()",
        )
    },
    **{
        signature: "0495_original_capabilities.sql"
        for signature in (
            "yoao_assets_v1.lock_original_item(uuid,boolean)",
            "yoao_assets_v1.task_original_source(uuid,boolean)",
            "yoao_assets_v1.auxiliary_original_source(uuid,boolean)",
            "yoao_assets_v1.publish_task_original_source(uuid)",
            "yoao_assets_v1.attach_verified_original(uuid,uuid)",
        )
    },
    **{
        signature: "0501_identity_avatar_uploads.sql"
        for signature in (
            *IDENTITY_AVATAR_FUNCTIONS,
            "yoao_assets_v1.matches_avatar_reservation(text,jsonb)",
        )
    },
    **{
        signature: "0502_jobs_ordinary_ledger.sql"
        for signature in (
            "yoao_jobs_v1.dataset_job_visible(uuid,uuid)",
            "yoao_jobs_v1._directory_job_ids(uuid,uuid)",
            "yoao_jobs_v1.directory_job_page(uuid,uuid,integer,integer)",
            "yoao_jobs_v1.directory_job_total(uuid,uuid)",
            "yoao_jobs_v1.dataset_active_job_facts(uuid)",
            "yoao_jobs_v1.active_storage_writer_job_counts()",
            "yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb)",
            "yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb)",
        )
    },
    **{signature: "0503_source_saved_io.sql" for signature in SAVED_SOURCE_FUNCTIONS},
    **{
        signature: "0504_reference_io.sql"
        for signature in (
            "yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb)",
            "yoao_assets_v1.capture_reference_io(uuid,text)",
            "yoao_assets_v1.recheck_reference_io(uuid,text)",
        )
    },
    **{
        signature: "0505_saved_outputs_source.sql"
        for signature in (
            "yoao_assets_v1.saved_output_origin(jsonb,text,text)",
            "yoao_assets_v1.saved_avatar_subject(text,uuid,text)",
            "yoao_assets_v1.saved_bug_subject(uuid,uuid,text,uuid)",
            "yoao_assets_v1.saved_archive_subject(uuid,uuid,text)",
            "yoao_assets_v1.begin_saved_output_write(uuid,text,text,jsonb)",
            "yoao_assets_v1.finish_saved_output_write(uuid,text,text,jsonb)",
            "yoao_assets_v1.saved_output_write_status(uuid,text,text)",
        )
    },
    **{
        signature: "0506_media_io.sql"
        for signature in (
            "yoao_assets_v1.media_io_artifact(text,text,uuid,uuid,boolean)",
            "yoao_assets_v1.media_io_cache_read(text,uuid,text,uuid)",
            "yoao_assets_v1.media_io_original_read(text,uuid,uuid)",
            "yoao_assets_v1.media_io_creation(uuid,uuid,uuid,text,boolean)",
            "yoao_assets_v1.media_io_creation_evidence(uuid,uuid,text,jsonb)",
            "yoao_assets_v1.media_io_thumbnail(text,uuid,uuid,boolean)",
            "yoao_assets_v1.begin_saved_operator_original(jsonb)",
        )
    },
    **{
        signature: "0507_training_saved_io.sql"
        for signature in (
            "yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb)",
            "yoao_assets_v1.record_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb,jsonb)",
        )
    },
    **{
        signature: "0508_migration_saved_io.sql"
        for signature in (
            "yoao_assets_v1.migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text)",
            "yoao_assets_v1.migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text)",
            "yoao_assets_v1.migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text)",
            "yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean)",
            "yoao_assets_v1.migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer)",
        )
    },
    **{
        signature: "0509_import_saved_io.sql"
        for signature in (
            *LEASED_IMPORT_FUNCTIONS,
            "yoao_assets_v1.import_saved_io_list(uuid,uuid,jsonb,jsonb)",
            "yoao_assets_v1.import_saved_io_action(jsonb,text,jsonb,jsonb)",
        )
    },
    **{
        signature: "0516_image_reference_manifest_members.sql"
        for signature in (
            "yoao_assets_v1._image_manifest_reference_matches(jsonb,jsonb,jsonb)",
            "yoao_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb)",
            "yoao_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text)",
            "yoao_assets_v1.is_sealed_image_reference_member(uuid,uuid,uuid,uuid,text,uuid,text,integer,bigint)",
        )
    },
    **{
        signature: "0517_jobs_list_platform_storage.sql"
        for signature in (
            "yoao_jobs_v1._async_job_list_rows_scoped(uuid,text,text[],text[],uuid,text,boolean)",
            "yoao_jobs_v1.async_job_list_page_scoped(uuid,text,text[],text[],uuid,text,integer,integer,boolean)",
            "yoao_jobs_v1.async_job_list_total_scoped(uuid,text,text[],text[],uuid,text,boolean)",
        )
    },
}
AUDIT_APPEND_ROLE = "yoao_audit_append_client"
AUDIT_QUERY_ROLE = "yoao_audit_query_client"
NOTIFICATION_READ_ROLE = "yoao_notification_read_client"
AUDIT_READ_ROLE = "yoao_audit_read_client"
AUDIT_DAILY_REFRESH_ROLE = "yoao_audit_daily_refresh_client"
AUDIT_MAINTENANCE_ROLE = "yoao_audit_maintenance_client"
JOBS_NOTIFICATION_SCOPE_ROLE = "yoao_jobs_notification_scope_client"
READ_RELATION_SPECS = {
    "yoao_labeling_v1.scene_track_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("project_id", "uuid"),
            ("scene_id", "uuid"),
            ("track_id", "character varying(64)"),
            ("class_name", "character varying(100)"),
            ("presence_mode", "character varying(16)"),
            ("attributes", "jsonb"),
            ("attributes_meta", "jsonb"),
            ("revision", "integer"),
            ("created_by", "uuid"),
            ("retired_at", "timestamp with time zone"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.scene_track_interval_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("scene_track_id", "uuid"),
            ("start_frame", "integer"),
            ("end_frame", "integer"),
            ("source", "character varying(30)"),
            ("version", "integer"),
            ("created_by", "uuid"),
            ("operation_id", "uuid"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.video_segment_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("dataset_item_id", "uuid"),
            ("segment_index", "integer"),
            ("start_frame", "integer"),
            ("end_frame", "integer"),
            ("assignee_id", "uuid"),
            ("status", "character varying(20)"),
            ("locked_by", "uuid"),
            ("locked_at", "timestamp with time zone"),
            ("lock_expires_at", "timestamp with time zone"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.video_tracker_job_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("dataset_item_id", "uuid"),
            ("annotation_id", "uuid"),
            ("segment_id", "uuid"),
            ("target_class_name", "character varying(100)"),
            ("target_tool_unit_id", "character varying(30)"),
            ("created_by", "uuid"),
            ("status", "character varying(20)"),
            ("job_kind", "character varying(20)"),
            ("track_id_snapshot", "character varying(64)"),
            ("correction_frame", "integer"),
            ("model_key", "character varying(80)"),
            ("direction", "character varying(20)"),
            ("from_frame", "integer"),
            ("to_frame", "integer"),
            ("prompt", "jsonb"),
            ("staged_result", "jsonb"),
            ("revision", "integer"),
            ("event_channel", "character varying(160)"),
            ("celery_task_id", "character varying(64)"),
            ("cancel_requested_at", "timestamp with time zone"),
            ("started_at", "timestamp with time zone"),
            ("completed_at", "timestamp with time zone"),
            ("error_message", "text"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.video_track_quality_run_rows": {
        "sql": "0428_video_export_core_reads.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("left_segment_id", "uuid"),
            ("right_segment_id", "uuid"),
            ("async_job_id", "uuid"),
            ("requested_by_id", "uuid"),
            ("status", "character varying(24)"),
            ("progress_pct", "integer"),
            ("input_snapshot", "jsonb"),
            ("input_digest", "character varying(64)"),
            ("sampling_digest", "character varying(64)"),
            ("metrics", "jsonb"),
            ("pairs", "jsonb"),
            ("error_message", "text"),
            ("accepted_by_id", "uuid"),
            ("accepted_at", "timestamp with time zone"),
            ("stale_at", "timestamp with time zone"),
            ("started_at", "timestamp with time zone"),
            ("completed_at", "timestamp with time zone"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.task_rows": {
        "sql": "0371_labeling_core_main_integration.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("project_id", "uuid"),
            ("dataset_item_id", "uuid"),
            ("display_id", "character varying(30)"),
            ("file_name", "character varying(500)"),
            ("file_path", "character varying(1000)"),
            ("file_type", "character varying(20)"),
            ("media_source_generation", "bigint"),
            ("media_object_identity", "jsonb"),
            ("media_source_drifted", "boolean"),
            ("tags", "jsonb"),
            ("status", "character varying(30)"),
            ("assignee_id", "uuid"),
            ("assignee_is_override", "boolean"),
            ("assigned_at", "timestamp with time zone"),
            ("is_labeled", "boolean"),
            ("overlap", "integer"),
            ("total_annotations", "integer"),
            ("total_predictions", "integer"),
            ("precomputed_agreement", "double precision"),
            ("sequence_order", "integer"),
            ("thumbnail_path", "character varying(512)"),
            ("blurhash", "character varying(64)"),
            ("batch_id", "uuid"),
            ("version", "integer"),
            ("submitted_at", "timestamp with time zone"),
            ("reviewer_id", "uuid"),
            ("reviewer_is_override", "boolean"),
            ("reviewer_claimed_at", "timestamp with time zone"),
            ("review_round_id", "uuid"),
            ("first_review_eligible", "boolean"),
            ("first_reviewed_at", "timestamp with time zone"),
            ("first_review_result", "character varying(16)"),
            ("first_review_contributor_ids", "jsonb"),
            ("annotation_contributor_ids", "jsonb"),
            ("review_contributor_ids", "jsonb"),
            ("review_submitter_id", "uuid"),
            ("reviewed_at", "timestamp with time zone"),
            ("reject_reason", "character varying(2000)"),
            ("reject_reason_type", "character varying(20)"),
            ("skip_reason", "character varying(50)"),
            ("skipped_at", "timestamp with time zone"),
            ("reopened_count", "integer"),
            ("last_reopened_at", "timestamp with time zone"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
            ("thumbnail_output_id", "uuid"),
            ("original_id", "uuid"),
        ],
    },
    "yoao_labeling_v1.batch_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("project_id", "uuid"),
            ("dataset_id", "uuid"),
            ("display_id", "character varying(30)"),
            ("name", "character varying(255)"),
            ("description", "text"),
            ("status", "character varying(30)"),
            ("priority", "integer"),
            ("deadline", "date"),
            ("assigned_user_ids", "jsonb"),
            ("total_tasks", "integer"),
            ("completed_tasks", "integer"),
            ("review_tasks", "integer"),
            ("approved_tasks", "integer"),
            ("rejected_tasks", "integer"),
            ("created_by", "uuid"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
            ("review_feedback", "text"),
            ("reviewed_at", "timestamp with time zone"),
            ("reviewed_by", "uuid"),
            ("annotator_id", "uuid"),
            ("reviewer_id", "uuid"),
            ("admin_locked", "boolean"),
            ("admin_lock_reason", "character varying(500)"),
            ("admin_locked_at", "timestamp with time zone"),
            ("admin_locked_by", "uuid"),
        ],
    },
    "yoao_labeling_v1.annotation_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("project_id", "uuid"),
            ("video_segment_id", "uuid"),
            ("user_id", "uuid"),
            ("source", "character varying(20)"),
            ("annotation_type", "character varying(30)"),
            ("tool_unit_id", "character varying(30)"),
            ("class_name", "character varying(100)"),
            ("geometry", "jsonb"),
            ("confidence", "double precision"),
            ("parent_prediction_id", "uuid"),
            ("parent_annotation_id", "uuid"),
            ("track_id", "character varying(64)"),
            ("scene_track_id", "uuid"),
            ("sensor_dataset_item_id", "uuid"),
            ("sensor_role", "character varying(50)"),
            ("sensor_visibility", "character varying(16)"),
            ("calibration_revision", "integer"),
            ("calibration_digest", "character varying(64)"),
            ("temporal_role", "character varying(16)"),
            ("lead_time", "double precision"),
            ("was_cancelled", "boolean"),
            ("ground_truth", "boolean"),
            ("is_active", "boolean"),
            ("attributes", "jsonb"),
            ("attributes_meta", "jsonb"),
            ("z_order", "integer"),
            ("is_locked", "boolean"),
            ("is_hidden", "boolean"),
            ("version", "integer"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.prediction_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("project_id", "uuid"),
            ("ml_backend_id", "uuid"),
            ("ml_backend_pool_id", "uuid"),
            ("model_version", "character varying(100)"),
            ("score", "double precision"),
            ("tool_unit_id", "character varying(30)"),
            ("result", "jsonb"),
            ("cluster", "integer"),
            ("mislabeling", "double precision"),
            ("source", "character varying(20)"),
            ("rejected_shape_indexes", "jsonb"),
            ("created_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.prediction_meta_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("prediction_id", "uuid"),
            ("prediction_created_at", "timestamp with time zone"),
            ("failed_prediction_id", "uuid"),
            ("inference_time_ms", "integer"),
            ("prompt_tokens", "integer"),
            ("completion_tokens", "integer"),
            ("total_tokens", "integer"),
            ("prompt_cost", "double precision"),
            ("completion_cost", "double precision"),
            ("total_cost", "double precision"),
            ("extra", "jsonb"),
            ("created_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.failed_prediction_rows": {
        "sql": "0371_labeling_core_main_integration.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("project_id", "uuid"),
            ("ml_backend_id", "uuid"),
            ("ml_backend_pool_id", "uuid"),
            ("model_version", "character varying(100)"),
            ("error_type", "character varying(100)"),
            ("message", "text"),
            ("retry_count", "integer"),
            ("last_retry_at", "timestamp with time zone"),
            ("dismissed_at", "timestamp with time zone"),
            ("extra", "jsonb"),
            ("created_at", "timestamp with time zone"),
            ("execution_snapshot_id", "uuid"),
        ],
    },
    "yoao_labeling_v1.task_lock_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("user_id", "uuid"),
            ("expire_at", "timestamp with time zone"),
            ("unique_id", "uuid"),
            ("created_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.task_dataset_item_link_rows": {
        "sql": "0352_labeling_core_read.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("dataset_item_id", "uuid"),
            ("role", "character varying(50)"),
            ("sensor_name", "character varying(100)"),
            ("created_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.annotation_draft_rows": {
        "sql": "0361_labeling_compatibility_reads.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
        ],
    },
    "yoao_assets_v1.labeling_dataset_item_facts": {
        "sql": "0361_labeling_compatibility_reads.sql",
        "owner": "assets_code",
        "columns": [
            ("id", "uuid"),
            ("dataset_id", "uuid"),
            ("file_path", "character varying(1000)"),
            ("file_type", "character varying(20)"),
            ("file_size", "bigint"),
            ("content_hash", "character varying(64)"),
            ("width", "integer"),
            ("height", "integer"),
        ],
    },
    "yoao_assets_v1.fixture_screenshot_item_facts": {
        "sql": "0432_assets_screenshot_facts.sql",
        "owner": "assets_code",
        "columns": [
            ("run_id", "uuid"),
            ("id", "uuid"),
            ("dataset_id", "uuid"),
            ("file_path", "character varying(1000)"),
            ("relative_path", "text"),
        ],
    },
    "yoao_assets_v1.scale_seed_dataset_item_facts": {
        "sql": "0364_labeling_scale_seed_operations.sql",
        "owner": "assets_code",
        "columns": [
            ("id", "uuid"),
            ("dataset_id", "uuid"),
            ("file_name", "character varying(500)"),
        ],
    },
    "yoao_assets_v1.video_chapter_facts": {
        "sql": "0354_labeling_spatial_quality.sql",
        "owner": "assets_code",
        "columns": [
            ("id", "uuid"),
            ("dataset_item_id", "uuid"),
            ("title", "character varying(200)"),
            ("start_frame", "integer"),
            ("end_frame", "integer"),
        ],
    },
    "yoao_notifications_v1.notification_facts": {
        "sql": "0319_notifications_read_relations.sql",
        "owner": "notifications_code",
        "columns": [
            ("id", "uuid"),
            ("user_id", "uuid"),
            ("type", "character varying(60)"),
            ("target_type", "character varying(30)"),
            ("target_id", "uuid"),
            ("payload", "jsonb"),
            ("read_at", "timestamp with time zone"),
            ("created_at", "timestamp with time zone"),
        ],
    },
    "yoao_audit_v1.audit_events": {
        "sql": "0320_audit_read_relations.sql",
        "owner": "audit_code",
        "columns": [
            ("id", "bigint"),
            ("actor_id", "uuid"),
            ("actor_email", "character varying(255)"),
            ("actor_role", "character varying(32)"),
            ("action", "character varying(64)"),
            ("target_type", "character varying(32)"),
            ("target_id", "character varying(64)"),
            ("method", "character varying(8)"),
            ("path", "character varying(256)"),
            ("status_code", "smallint"),
            ("ip", "inet"),
            ("detail_json", "jsonb"),
            ("request_id", "character varying(36)"),
            ("created_at", "timestamp with time zone"),
        ],
    },
    "yoao_audit_v1.audit_daily_facts": {
        "sql": "0322_audit_daily_projection.sql",
        "owner": "audit_code",
        "columns": [
            ("day", "date"),
            ("action", "character varying(64)"),
            ("target_type", "character varying"),
            ("actor_role", "character varying"),
            ("status_family", "smallint"),
            ("event_count", "bigint"),
        ],
    },
    "yoao_jobs_v1.notification_scope_facts": {
        "sql": "0323_jobs_notification_scope_facts.sql",
        "owner": "jobs_code",
        "columns": [
            ("id", "uuid"),
            ("kind", "character varying(40)"),
            ("scope_project_id", "uuid"),
        ],
    },
    "yoao_labeling_v1.annotation_comment_facts": {
        "sql": "0347_labeling_discussion.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("annotation_id", "uuid"),
            ("project_id", "uuid"),
            ("author_id", "uuid"),
            ("body", "text"),
            ("is_resolved", "boolean"),
            ("is_active", "boolean"),
            ("mentions", "jsonb"),
            ("attachments", "jsonb"),
            ("canvas_drawing", "jsonb"),
            ("anchor", "jsonb"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.annotation_feedback_facts": {
        "sql": "0347_labeling_discussion.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("kind", "character varying(16)"),
            ("anchor_type", "character varying(16)"),
            ("project_id", "uuid"),
            ("task_id", "uuid"),
            ("annotation_id", "uuid"),
            ("anchor_position", "jsonb"),
            ("status", "character varying(16)"),
            ("severity", "character varying(16)"),
            ("title", "character varying(500)"),
            ("body", "text"),
            ("author_id", "uuid"),
            ("attachments", "jsonb"),
            ("mentions", "jsonb"),
            ("canvas_drawing", "jsonb"),
            ("thread_parent_id", "uuid"),
            ("is_active", "boolean"),
            ("resolved_at", "timestamp with time zone"),
            ("resolved_by_id", "uuid"),
            ("created_at", "timestamp with time zone"),
            ("updated_at", "timestamp with time zone"),
        ],
    },
    "yoao_labeling_v1.task_event_facts": {
        "sql": "0347_labeling_discussion.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
            ("user_id", "uuid"),
            ("project_id", "uuid"),
            ("kind", "character varying(16)"),
            ("started_at", "timestamp with time zone"),
            ("ended_at", "timestamp with time zone"),
            ("duration_ms", "integer"),
            ("annotation_count", "integer"),
            ("was_rejected", "boolean"),
            ("collector_version", "character varying(32)"),
            ("collection_source", "character varying(32)"),
            ("collection_coverage", "character varying(32)"),
            ("created_at", "timestamp with time zone"),
            ("reject_reason_type", "character varying(20)"),
        ],
    },
}
IDENTITY_CLIENT_ROLES = {
    "INFERENCE_POOL_REFERENCE": "yoao_inference_pool_reference_client",
    "LABELING_VIDEO_CONTROL": "yoao_labeling_video_control_client",
    "LABELING_ANNOTATION_TRANSFER": "yoao_labeling_annotation_transfer_client",
    "LABELING_CORE_READ": "yoao_labeling_core_read_client",
    "LABELING_MASK_QUALITY": "yoao_labeling_mask_quality_client",
    "LABELING_SPATIAL_QUALITY": "yoao_labeling_spatial_quality_client",
    "LABELING_TASK_MAINTENANCE": "yoao_labeling_task_maintenance_client",
    "AUTH": "yoao_identity_auth_client",
    "ACTOR_STATUS": "yoao_identity_actor_status_client",
    "COMMAND": "yoao_identity_command_client",
    "FENCE": "yoao_identity_fence_client",
    "OPERATOR": "yoao_identity_session_operator",
    "KEYS": "yoao_identity_key_client",
    "SESSIONS": "yoao_identity_session_client",
    "ACCOUNTS": "yoao_identity_account_client",
    "GROUP": "yoao_identity_group_client",
    "INVITATION": "yoao_identity_invitation_client",
    "DEACTIVATION": "yoao_identity_deactivation_client",
    "DEACTIVATION_OPERATOR": "yoao_identity_deactivation_operator",
    "NOTIFICATION_PUBLISH": "yoao_notification_publish_client",
    "PRESENCE": "yoao_identity_presence_client",
    "RECOVERY": "yoao_identity_recovery_client",
    "MAIL": "yoao_identity_mail_client",
    "MAIL_V2": "yoao_identity_mail_v2_client",
    "MAIL_SETTINGS": "yoao_identity_mail_settings_client",
    "ADMIN_PASSWORD": "yoao_identity_admin_password_client",
    "PLATFORM_ROLE": "yoao_identity_platform_role_client",
    "SUSPENSION": "yoao_identity_suspension_client",
    "DELETION": "yoao_identity_deletion_client",
    "OFFBOARDING": "yoao_identity_offboarding_client",
    "DIRECTORY": "yoao_identity_directory_client",
    "BRIEFS": "yoao_identity_briefs_client",
    "PROVISIONING": "yoao_identity_provisioning_client",
    "WORKSPACE_MEMBERSHIP": "yoao_workspace_membership_client",
    "WORKSPACE_OWNER_TRANSFER": "yoao_workspace_owner_transfer_client",
    "WORKSPACE_PROJECT_MAINTENANCE": "yoao_workspace_project_maintenance_client",
    "WORKSPACE_PROJECT_DELETION": "yoao_workspace_project_deletion_client",
    "WORKSPACE_DATASET_UNLINK": "yoao_workspace_dataset_unlink_client",
    "WORKSPACE_CONFIGURATION": "yoao_workspace_configuration_client",
    "WORKSPACE_TEMPLATE_QUERY": "yoao_workspace_template_query_client",
    "WORKSPACE_PROJECT_QUERY": "yoao_workspace_project_query_client",
    "WORKSPACE_DIRECTORY_QUERY": "yoao_workspace_directory_query_client",
    "LABELING_CLASS_QUERY": "yoao_labeling_class_query_client",
    "LABELING_VIDEO_WRITER": "yoao_labeling_video_writer_client",
    "LABELING_TASK_CONTROL": "yoao_labeling_task_control_client",
    "LABELING_ANNOTATION_WRITE": "yoao_labeling_annotation_write_client",
    "LABELING_EDITING": "yoao_labeling_editing_client",
    "LABELING_BATCH_CONTROL": "yoao_labeling_batch_control_client",
    "LABELING_PREDICTION": "yoao_labeling_prediction_client",
    "LABELING_SCENE_TRACK_COMMAND": "yoao_labeling_scene_track_command_client",
    "LABELING_CROSS_FRAME": "yoao_labeling_cross_frame_client",
    "LABELING_ANNOTATION_MAINTENANCE": "yoao_labeling_annotation_maintenance_client",
    "LABELING_DISCUSSION": "yoao_labeling_discussion_client",
    "LABELING_MASK_EDIT": "yoao_labeling_mask_edit_client",
    "LABELING_SCENE_READ": "yoao_labeling_scene_read_client",
    "VIDEO_RESTORE": "yoao_video_restore_operator_client",
    "INFERENCE_PIPELINES": "yoao_inference_pipelines_client",
    "INFERENCE_BACKEND_PROBE": "yoao_inference_backend_probe_client",
    "INFERENCE_HEALTH_OBSERVATION": "yoao_inference_health_observation_client",
    "INFERENCE_HEALTH_ORIGIN": "yoao_inference_health_origin_client",
    "INFERENCE_HEALTH_DATA": "yoao_inference_health_data_client",
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
    "yoao_append_client",
    "yoao_asset_cleanup_client",
    INFERENCE_REGISTRY_INGRESS_ROLE,
    INFERENCE_ROUTING_ROLE,
    "yoao_asset_catalog_client",
    "yoao_asset_source_client",
    "yoao_asset_import_client",
    "yoao_asset_operator_client",
    "yoao_asset_media_control_client",
    "yoao_asset_media_worker_client",
    "yoao_asset_image_worker_client",
    "yoao_labeling_mask_gc_client",
    "yoao_preannotation_result_client",
    VIDEO_EXECUTION_ROLE,
    NOTIFICATION_PREFERENCES_ROLE,
    NOTIFICATION_APPEND_ROLE,
    NOTIFICATION_INBOX_ROLE,
    NOTIFICATION_DELIVERY_READ_ROLE,
    AUDIT_APPEND_ROLE,
    AUDIT_QUERY_ROLE,
    NOTIFICATION_READ_ROLE,
    AUDIT_READ_ROLE,
    AUDIT_DAILY_REFRESH_ROLE,
    AUDIT_MAINTENANCE_ROLE,
    JOBS_NOTIFICATION_SCOPE_ROLE,
    JOBS_LIST_QUERY_ROLE,
    JOBS_LIFECYCLE_ROLE,
    JOBS_DISPATCH_RELAY_ROLE,
    JOBS_PUBLICATION_ROLE,
    EXPORT_EXECUTION_ROLE,
    *IDENTITY_CLIENT_ROLES.values(),
)
JOBS_SQL_ARTIFACTS = (
    "0447_jobs_publication_execution.sql",
    "0447_jobs_fixture_media_publication.sql",
    "0451_jobs_publication_retirement.sql",
)
WORKER_JOB_SCOPE_SQL = (
    "0465_worker_job_dataset_receipts.sql",
    "0465_retry_born_dataset_scope.sql",
    "0465_cross_frame_born_dataset_scope.sql",
    "0465_worker_job_dataset_scope.sql",
)
ASSETS_SOURCE_SQL_ARTIFACTS = ("0453_import_original_verification.sql",)
PACKAGED_CURRENT_SQL = {
    "0489_inference_prediction_runtime.sql": "apps/inference/src/yoao_inference/sql",
    "0489_prediction_input_leases.sql": "apps/assets-media/src/yoao_assets_media/sql",
    "0489_prediction_retry_pipeline_failure.sql": "apps/labeling/src/yoao_labeling/sql",
    "0495_original_capabilities.sql": "apps/assets-source/src/yoao_assets_source",
    "0502_jobs_ordinary_ledger.sql": "apps/jobs/src/yoao_jobs/sql",
    "0510_notification_channels.sql": "apps/notifications/src/yoao_notifications/sql",
}
INSIGHTS_STORAGE_SQL = (
    "0487_insights_perf_refresh.sql",
    "0487_insights_task_views.sql",
    "0487_insights_dataset_views.sql",
    "0487_insights_storage_owner.sql",
)
CURRENT_NAMESPACE_SQL = (
    "0480_fixture_annotation_import.sql",
    "0481_fixture_video_requests.sql",
    "0483_fixture_prediction_import.sql",
    "0484_notifications_terminal_outbox.sql",
    "0485_audit_platform_maintenance.sql",
    *INSIGHTS_STORAGE_SQL,
    *PACKAGED_CURRENT_SQL,
    "0499_labeling_artifact_boundaries.sql",
    "0501_identity_avatar_uploads.sql",
    "0503_source_saved_io.sql",
    "0504_reference_io.sql",
    "0505_saved_outputs_source.sql",
    "0506_media_io.sql",
    "0507_training_saved_io.sql",
    "0508_migration_saved_io.sql",
    "0509_import_saved_io.sql",
    "0510_notification_target_visibility.sql",
    "0513_inference_plugin_direct_mode.sql",
    "0516_image_reference_manifest_members.sql",
    "0517_jobs_list_platform_storage.sql",
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
    "0251_identity_account_locks.sql",
    "0252_identity_provisioning_support.sql",
    "0252_identity_provisioning.sql",
    "0253_workspace_membership_labeling.sql",
    "0253_workspace_membership_support.sql",
    "0253_workspace_membership.sql",
    "0254_workspace_owner_transfer.sql",
    "0255_workspace_configuration_dependencies.sql",
    "0256_workspace_configuration_support.sql",
    "0257_workspace_configuration.sql",
    "0258_workspace_template_query_support.sql",
    "0259_workspace_template_queries.sql",
    "0260_workspace_project_query_support.sql",
    "0261_workspace_project_queries.sql",
    "0262_workspace_member_authority_temporals.sql",
    "0263_workspace_directory_queries.sql",
    "0264_workspace_access_queries.sql",
    "0265_workspace_manager_queries.sql",
    "0266_labeling_class_query.sql",
    "0267_labeling_class_rename.sql",
    "0268_workspace_class_rename.sql",
    "0269_workspace_guide_assets.sql",
    "0270_workspace_project_access_step.sql",
    "0271_inference_pipelines.sql",
    "0272_workspace_backend_bindings.sql",
    "0273_inference_backend_dependency_reads.sql",
    "0274_inference_backend_probe.sql",
    "0275_workspace_binding_lock_guard.sql",
    "0276_inference_health_observation.sql",
    "0277_inference_health_data.sql",
    "0278_inference_registry_ingress.sql",
    "0279_inference_routing.sql",
    "0280_inference_scope_retirement.sql",
    "0281_video_execution.sql",
    "0282_video_execution_checkpoints.sql",
    "0283_labeling_video_writer.sql",
    "0284_video_restore.sql",
    "0285_identity_accepted_api_key.sql",
    "0286_identity_read_relations.sql",
    "0287_identity_registration_status.sql",
    "0288_workspace_project_maintenance.sql",
    "0289_workspace_access_read.sql",
    "0290_workspace_project_lock.sql",
    "0291_workspace_reference_nowait.sql",
    "0292_workspace_member_locks.sql",
    "0293_workspace_project_read.sql",
    "0294_workspace_project_set_lock.sql",
    "0295_labeling_project_counter_write.sql",
    "0296_workspace_actor_membership.sql",
    "0297_workspace_member_facts.sql",
    "0298_workspace_project_facts.sql",
    "0299_labeling_project_counter_deltas.sql",
    "0300_video_deletion_retirement.sql",
    "0301_workspace_seed_project.sql",
    "0302_identity_seed_persona.sql",
    "0302_workspace_seed_members.sql",
    "0303_identity_scale_seed_actors.sql",
    "0304_identity_fixture_preferences.sql",
    "0305_identity_e2e_fixtures.sql",
    "0306_gpu_registry_delete_noop.sql",
    "0307_workspace_media_seed_project.sql",
    "0308_workspace_seed_member_assignment.sql",
    "0309_workspace_e2e_fixtures.sql",
    "0310_workspace_scale_seed.sql",
    "0311_notifications_preferences.sql",
    "0312_video_tracker_input_retention.sql",
    "0313_video_tracker_input_garbage_retention.sql",
    "0314_notifications_append.sql",
    "0315_audit_append.sql",
    "0316_notifications_read_state.sql",
    "0317_audit_query.sql",
    "0318_notifications_delete.sql",
    "0319_notifications_read_relations.sql",
    "0320_audit_read_relations.sql",
    "0321_notifications_terminal_append.sql",
    "0322_audit_daily_projection.sql",
    "0323_jobs_notification_scope_facts.sql",
    "0324_notifications_inbox_query.sql",
    "0325_notifications_delivery_read.sql",
    "0326_jobs_list_query.sql",
    "0327_jobs_lifecycle.sql",
    "0328_jobs_row_locks.sql",
    "0329_jobs_domain_queries.sql",
    "0330_jobs_fixture_cleanup.sql",
    "0331_jobs_filter_fixtures.sql",
    "0332_jobs_action_idempotency.sql",
    "0333_audit_maintenance.sql",
    "0334_labeling_task_locks.sql",
    "0335_labeling_task_workflow.sql",
    "0336_labeling_annotation_statistics.sql",
    "0337_labeling_scene_track_domain.sql",
    "0338_labeling_annotation_revision_support.sql",
    "0338_labeling_annotation_writes.sql",
    "0339_labeling_annotation_bulk.sql",
    "0340_labeling_video_bbox_editing.sql",
    "0341_labeling_batch_control.sql",
    "0342_labeling_prediction_records.sql",
    "0343_labeling_scene_track_commands.sql",
    "0344_labeling_cross_frame_writes.sql",
    "0345_labeling_annotation_prediction.sql",
    "0346_labeling_annotation_maintenance.sql",
    "0347_labeling_discussion.sql",
    "0348_labeling_mask_edits.sql",
    "0349_labeling_scene_read.sql",
    "0350_labeling_video_control.sql",
    "0351_labeling_annotation_transfer.sql",
    "0352_labeling_core_read.sql",
    "0353_labeling_mask_quality.sql",
    "0354_labeling_spatial_quality.sql",
    "0355_labeling_task_maintenance.sql",
    "0356_inference_pool_reference.sql",
    "0357_labeling_prediction_partitions.sql",
    "0358_labeling_fixture_cleanup.sql",
    "0359_labeling_upload_tasks.sql",
    "0360_labeling_video_segment_lifecycle.sql",
    "0361_labeling_compatibility_reads.sql",
    "0362_labeling_e2e_fixture_writes.sql",
    "0363_labeling_screenshot_operations.sql",
    "0364_labeling_scale_seed_operations.sql",
    "0366_audit_fixture_cleanup.sql",
    "0367_labeling_filtering_rendering.sql",
    "0368_jobs_dispatch_ledger.sql",
    "0369_inference_topology.sql",
    "0369_inference_seed_ingress.sql",
    "0369_inference_fixtures.sql",
    "0369_inference_backend_endpoint.sql",
    "0369_inference_health.sql",
    "0369_inference_gpu.sql",
    "0370_inference_gpu.sql",
    "0371_labeling_core_main_integration.sql",
    "0371_assets_owner_main_integration.sql",
    "0371_assets_publication_storage_gate.sql",
    "0371_identity_avatar_account_command.sql",
    "0371_notifications_main_integration.sql",
    "0371_project_deletion_storage_retirement.sql",
    "0371_inference_backend_endpoint_auth_revision.sql",
    "0371_inference_routing_auth_rotation.sql",
    "0371_media_owner_item_links.sql",
    "0372_prediction_failure_snapshot.sql",
    "0373_prediction_execution_snapshot_binding.sql",
    "0374_workspace_guide_publication.sql",
    "0375_original_authority_receipts.sql",
    "0376_assets_connection_runtime.sql",
    "0377_workspace_project_snapshots.sql",
    "0378_identity_avatar_authority.sql",
    "0379_operator_item_locator.sql",
    "0379_operator_auxiliary.sql",
    "0380_labeling_upload_task_retirement.sql",
    "0381_main_storage_invariant_search_path.sql",
    "0382_jobs_row_progress_facts.sql",
    "0383_labeling_task_delete_retention.sql",
    "0384_jobs_row_progress_write.sql",
    "0385_labeling_repair_rollback_shard.sql",
    "0386_inference_repair_model_lock.sql",
    "0387_labeling_annotation_lineage.sql",
    "0388_inference_plugin_installation.sql",
    "0388_inference_plugin_ingress.sql",
    "0389_inference_deployment_usage.sql",
    "0389_labeling_deployment_usage.sql",
    "0389_learning_deployment_retirement.sql",
    "0390_inference_gpu.sql",
    "0391_gpu_plugin_transition.sql",
    "0392_plugin_prepared_cancellation.sql",
    "0393_plugin_registry_admission_fence.sql",
    "0394_endpoint_routing_transport_identity.sql",
    "0395_plugin_gpu_retirement.sql",
    "0396_labeling_original_relations.sql",
    "0397_preannotation_result_receipt.sql",
    "0398_gpu_terminal_owner_recovery.sql",
    "0399_plugin_terminal_hold.sql",
    "0400_plugin_routing_replacement.sql",
    "0402_plugin_terminal_drain_route.sql",
    "0403_plugin_asset_approval.sql",
    "0404_route_scope_supersession.sql",
    "0405_video_tracker_asset_candidates.sql",
    "0406_labeling_mask_decision_lock.sql",
    "0407_jobs_video_retry.sql",
    "0409_video_execution_settlement.sql",
    "0410_inference_managed_auth.sql",
    "0411_video_execution_physical_closure.sql",
    "0412_plugin_tracker_retirement_order.sql",
    "0413_tracker_retirement_function_settings.sql",
    "0414_export_core_locks.sql",
    "0415_export_job_nowait.sql",
    "0419_dataset_ingest_authority.sql",
    "0420_spatial_item_role_join.sql",
    "0421_notification_job_scope.sql",
    "0422_mask_revision_digest.sql",
    "0423_identity_presence_actor.sql",
    "0423_identity_presence.sql",
    "0424_upload_publication_identity.sql",
    "0425_identity_preferences.sql",
    "0425_identity_provisioning.sql",
    "0426_image_admission_share.sql",
    "0427_labeling_actor_batch_share.sql",
    "0428_video_export_core_reads.sql",
    "0429_video_export_pair.sql",
    "0431_labeling_mask_staging.sql",
    "0432_fixture_run_ownership_schema.sql",
    "0432_labeling_fixture_retry_guard.sql",
    "0432_jobs_fixture_guard.sql",
    "0432_jobs_fixture_dataset_guard.sql",
    "0432_fixture_jobs_anchor.sql",
    "0432_fixture_dataset_jobs_anchor.sql",
    "0432_fixture_dataset_identity.sql",
    "0432_assets_fixture_auxiliary.sql",
    "0432_assets_screenshot_facts.sql",
    "0432_identity_fixture_invitations.sql",
    "0432_identity_invitation_command.sql",
    "0432_labeling_screenshot_operations.sql",
    "0432_jobs_fixture_presentation.sql",
    "0432_assets_fixture_source_guard.sql",
    "0432_assets_fixture_reclaim_guard.sql",
    "0432_assets_scale_fixture_guard.sql",
    "0432_labeling_original_read.sql",
    "0432_assets_media_source_read.sql",
    "0432_assets_media_source_admission.sql",
    "0432_assets_spatial_original_admission.sql",
    "0432_assets_media_source_authority.sql",
    "0432_assets_images_source_authority.sql",
    "0432_assets_inputs_source_authority.sql",
    "0432_assets_video_inputs_source_authority.sql",
    "0432_assets_video_inputs_control_source.sql",
    "0432_assets_tracker_control_source.sql",
    "0433_gpu_membership_scalar_guard.sql",
    "0433_project_video_retirement.sql",
    "0434_prediction_execution_routing.sql",
    "0434_prediction_output_ownership.sql",
    "0435_fixture_export_retention.sql",
    "0435_fixture_export_zip_retirement.sql",
    "0435_fixture_export_retired.sql",
    "0435_fixture_export_execution.sql",
    "0435_fixture_native_export.sql",
    "0435_fixture_export_owner.sql",
    "0436_fixture_jobs_join.sql",
    "0437_fixture_audit_receipts.sql",
    "0437_fixture_audit_cleanup.sql",
    "0437_fixture_reclaim_job_scope.sql",
    "0438_fixture_audit_namespace.sql",
    "0439_inference_fixture_health_retirement.sql",
    "0440_fixture_auxiliary_retirement.sql",
    "0441_fixture_presentation_dataset_guard.sql",
    "0442_fixture_export_zip_versions.sql",
    "0443_fixture_dataset_retired_jobs.sql",
    "0444_image_export_scope.sql",
    "0444_image_export_dataset_scope.sql",
    "0445_assets_fixture_reader_completion.sql",
    "0446_fixture_media_births.sql",
    "0446_fixture_original_readers.sql",
    "0446_fixture_independent_source.sql",
    "0446_media_producer_binding.sql",
    "0446_fixture_independent_birth.sql",
    "0446_fixture_native_source_ownership.sql",
    "0446_fixture_native_media_jobs.sql",
    "0446_fixture_media_references.sql",
    "0446_fixture_media_jobs.sql",
    "0446_fixture_media_execution.sql",
    "0446_fixture_native_media_admissions.sql",
    "0446_fixture_native_media_sources.sql",
    "0446_fixture_native_media_outputs.sql",
    "0446_fixture_native_target_fence.sql",
    "0446_fixture_native_target_references.sql",
    "0446_fixture_native_media_execution.sql",
    "0446_fixture_native_media_scan.sql",
    "0446_fixture_native_media_scans.sql",
    "0446_fixture_native_media_backfill_source.sql",
    "0446_fixture_native_media_claims.sql",
    "0446_fixture_native_media_reads.sql",
    "0446_fixture_native_runtime_ready.sql",
    "0446_fixture_native_media_resolver.sql",
    "0446_fixture_native_media_garbage.sql",
    "0446_fixture_media_children.sql",
    "0446_fixture_media_pyramid_attempts.sql",
    "0446_fixture_native_pyramid.sql",
    "0446_fixture_media_video_attempts.sql",
    "0446_fixture_media_dispatch.sql",
    "0446_fixture_media_versions.sql",
    "0446_fixture_independent_execution.sql",
    "0446_fixture_legacy_birth_admission.sql",
    "0446_fixture_screenshot_producer.sql",
    "0446_fixture_independent_pyramids.sql",
    "0446_fixture_independent_video.sql",
    "0446_fixture_screenshot_output_receipt.sql",
    "0446_media_native_stages.sql",
    "0446_fixture_media_permit_fence.sql",
    "0446_fixture_media_retirement.sql",
    "0446_fixture_independent_retirement.sql",
    "0446_fixture_native_media_retirement.sql",
    "0446_fixture_native_job_retirement.sql",
    "0446_fixture_media_output_permits.sql",
    "0446_fixture_media_version_retirement.sql",
    "0446_fixture_media_output_inventory.sql",
    "0446_fixture_media_output_fence.sql",
    "0446_fixture_media_no_birth.sql",
    "0446_fixture_media_retired.sql",
    "0446_fixture_media_jobs_retired.sql",
    "0446_fixture_native_target_retirement.sql",
    "0446_fixture_video_anchors.sql",
    "0446_fixture_native_target_ack.sql",
    "0446_fixture_media_dataset_guards.sql",
    *JOBS_SQL_ARTIFACTS,
    "0447_export_execution.sql",
    "0447_export_fixture_rebind.sql",
    "0448_fixture_external_pyramid_reuse.sql",
    "0449_image_backfill_purpose.sql",
    "0450_video_candidate_status_restore.sql",
    *ASSETS_SOURCE_SQL_ARTIFACTS,
    "0454_identity_first_seed_account.sql",
    "0455_images_output_ownership.sql",
    "0456_prediction_dataset_scope.sql",
    "0457_inference_registry_seed_marker_revision.sql",
    "0458_upload_retry_after_failed_publication.sql",
    "0459_source_ingest_identity_and_retry.sql",
    "0460_workspace_owner_transfer_namespace.sql",
    "0461_fixture_native_pyramid_target.sql",
    "0462_fixture_native_pyramid_receipts.sql",
    "0463_native_pyramid_reads_retirement.sql",
    "0463_native_pyramid_projection.sql",
    "0463_native_pyramid_retirement.sql",
    "0464_native_pyramid_saved_targets.sql",
    "0464_native_storage_capture.sql",
    *WORKER_JOB_SCOPE_SQL,
    "0466_task_edit_lock_domain.sql",
    "0467_workspace_membership_namespace.sql",
    "0469_routing_admission_header.sql",
    "0470_diagnostics_insights_namespace.sql",
    "0471_workspace_query_namespace.sql",
    "0472_briefs_class_query_namespaces.sql",
    "0473_keys_gpu_capabilities.sql",
)
ASSETS_SOURCE_SCHEMA_ARTIFACTS = (
    "0453_import_original_completion.schema.json",
    "import_verification_contract.schema.json",
)
SCHEMA_ARTIFACTS = (
    "0445_assets_fixture_reader_completion.schema.json",
    "0437_fixture_audit_cleanup.schema.json",
    "0434_prediction_execution_routing.schema.json",
    "0434_prediction_output_ownership.schema.json",
    "0444_fixture_jobs.schema.json",
    "0465_fixture_jobs.schema.json",
    "0432_labeling_screenshot_operations.schema.json",
    "0419_dataset_ingest_authority.schema.json",
    "0371_assets_owner_main_integration.schema.json",
    "0371_assets_managed_originals.schema.json",
    "0377_workspace_project_snapshots.schema.json",
    "0377_asset_association_reads.schema.json",
    "0379_operator_item_locator.schema.json",
    "0362_labeling_e2e_fixture_writes.schema.json",
    "0367_labeling_filtering_rendering.schema.json",
    "0368_jobs_dispatch_ledger.schema.json",
    "0369_inference_topology.schema.json",
    "0410_inference_managed_auth.schema.json",
    "0388_inference_plugin_installation.schema.json",
    "0389_inference_deployment_usage.schema.json",
    "0389_labeling_deployment_usage.schema.json",
    "0369_inference_backend_endpoint.schema.json",
    "0369_inference_gpu.schema.json",
    "0390_inference_gpu.schema.json",
    "0363_labeling_screenshot_operations.schema.json",
    "0364_labeling_scale_seed_operations.schema.json",
    "0360_labeling_video_segment_lifecycle.schema.json",
    "0359_labeling_upload_tasks.schema.json",
    "0358_labeling_fixture_cleanup.schema.json",
    "0357_labeling_prediction_partitions.schema.json",
    "0356_inference_pool_reference.schema.json",
    "0350_labeling_video_control.schema.json",
    "0351_labeling_annotation_transfer.schema.json",
    "0431_labeling_mask_staging.schema.json",
    "0353_labeling_mask_quality.schema.json",
    "0354_labeling_spatial_quality.schema.json",
    "0355_labeling_task_maintenance.schema.json",
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
    "0251_identity_account_locks.schema.json",
    "0252_identity_provisioning.schema.json",
    "0253_workspace_membership.schema.json",
    "0254_workspace_owner_transfer.schema.json",
    "0255_workspace_configuration_dependencies.schema.json",
    "0256_workspace_configuration_support.schema.json",
    "0257_workspace_configuration.schema.json",
    "0258_workspace_template_query_support.schema.json",
    "0259_workspace_template_queries.schema.json",
    "0260_workspace_project_query_support.schema.json",
    "0261_workspace_project_queries.schema.json",
    "0262_workspace_member_authority_temporals.schema.json",
    "0263_workspace_directory_queries.schema.json",
    "0264_workspace_access_queries.schema.json",
    "0265_workspace_manager_queries.schema.json",
    "0266_labeling_class_query.schema.json",
    "0267_labeling_class_rename.schema.json",
    "0268_workspace_class_rename.schema.json",
    "0269_workspace_guide_assets.schema.json",
    "0270_workspace_project_access_step.schema.json",
    "0271_inference_pipelines.schema.json",
    "0272_workspace_backend_bindings.schema.json",
    "0273_inference_backend_dependency_reads.schema.json",
    "0274_inference_backend_probe.schema.json",
    "0275_workspace_binding_lock_guard.schema.json",
    "0276_inference_health_observation.schema.json",
    "0277_inference_health_data.schema.json",
    "0278_inference_registry_ingress.schema.json",
    "0279_inference_routing.schema.json",
    "0280_inference_scope_retirement.schema.json",
    "0281_video_execution.schema.json",
    "0282_video_execution_checkpoints.schema.json",
    "0283_labeling_video_writer.schema.json",
    "0284_video_restore.schema.json",
    "0285_identity_accepted_api_key.schema.json",
    "0286_identity_read_relations.schema.json",
    "0287_identity_registration_status.schema.json",
    "0288_workspace_project_maintenance.schema.json",
    "0289_workspace_access_read.schema.json",
    "0290_workspace_project_lock.schema.json",
    "0291_workspace_reference_nowait.schema.json",
    "0292_workspace_member_locks.schema.json",
    "0293_workspace_project_read.schema.json",
    "0294_workspace_project_set_lock.schema.json",
    "0295_labeling_project_counter_write.schema.json",
    "0296_workspace_actor_membership.schema.json",
    "0297_workspace_member_facts.schema.json",
    "0298_workspace_project_facts.schema.json",
    "0299_labeling_project_counter_deltas.schema.json",
    "0300_video_deletion_retirement.schema.json",
    "0301_workspace_seed_project.schema.json",
    "0302_identity_seed_persona.schema.json",
    "0302_workspace_seed_members.schema.json",
    "0303_identity_scale_seed_actors.schema.json",
    "0304_identity_fixture_preferences.schema.json",
    "0305_identity_e2e_fixtures.schema.json",
    "0307_workspace_media_seed_project.schema.json",
    "0308_workspace_seed_member_assignment.schema.json",
    "0309_workspace_e2e_fixtures.schema.json",
    "0310_workspace_scale_seed.schema.json",
    "0311_notifications_preferences.schema.json",
    "0312_video_tracker_input_retention.schema.json",
    "0313_video_tracker_input_garbage_retention.schema.json",
    "0314_notifications_append.schema.json",
    "0315_audit_append.schema.json",
    "0316_notifications_read_state.schema.json",
    "0317_audit_query.schema.json",
    "0318_notifications_delete.schema.json",
    "0319_notifications_read_relations.schema.json",
    "0320_audit_read_relations.schema.json",
    "0321_notifications_terminal_append.schema.json",
    "0322_audit_daily_projection.schema.json",
    "0323_jobs_notification_scope_facts.schema.json",
    "0324_notifications_inbox_query.schema.json",
    "0325_notifications_delivery_read.schema.json",
    "0326_jobs_list_query.schema.json",
    "0327_jobs_lifecycle.schema.json",
    "0328_jobs_row_locks.schema.json",
    "0415_jobs_row_locks.schema.json",
    "0329_jobs_domain_queries.schema.json",
    "0330_jobs_fixture_cleanup.schema.json",
    "0331_jobs_filter_fixtures.schema.json",
    "0332_jobs_action_idempotency.schema.json",
    "0333_audit_maintenance.schema.json",
    "0334_labeling_task_locks.schema.json",
    "0466_task_edit_lock_domain.schema.json",
    "0335_labeling_task_workflow.schema.json",
    "0336_labeling_annotation_statistics.schema.json",
    "0337_labeling_scene_track_domain.schema.json",
    "0338_labeling_annotation_writes.schema.json",
    "0339_labeling_annotation_bulk.schema.json",
    "0340_labeling_video_bbox_editing.schema.json",
    "0341_labeling_batch_control.schema.json",
    "0342_labeling_prediction_records.schema.json",
    "0343_labeling_scene_track_commands.schema.json",
    "0344_labeling_cross_frame_writes.schema.json",
    "0345_labeling_annotation_prediction.schema.json",
    "0346_labeling_annotation_maintenance.schema.json",
    "0347_labeling_discussion.schema.json",
    "0347_labeling_task_events.schema.json",
    "0348_labeling_mask_edits.schema.json",
    "0348_labeling_mask_revisions.schema.json",
    "0348_labeling_ai_mask_accept.schema.json",
    "0349_labeling_scene_read.schema.json",
    "0366_audit_fixture_cleanup.schema.json",
    "0454_identity_first_seed_account.schema.json",
    "0455_images_output_ownership.schema.json",
    *ASSETS_SOURCE_SCHEMA_ARTIFACTS,
)
_RECLAMATION_PROFILES = {
    "tests/test_managed_original_reclaim_fences.py::test_selected_group_and_global_pages_share_facts_but_not_scope": "reclaim-scope",
    "tests/test_managed_original_reclaim_fences.py::test_reconciliation_unknown_coverage_survives_empty_retirement_table": "reclaim-unknown",
    "tests/test_managed_original_reclaim_fences.py::test_only_valid_current_attempt_can_exclude_its_exact_operation": "reclaim-operation",
    "tests/test_managed_original_reclaim.py::test_observation_gets_do_not_claim_reclamation_in_read_only_transactions": "reclaim-read-only",
    "tests/test_managed_original_reclaim.py::test_http_preview_commit_and_worker_real_small_version_delete": "reclaim-version-delete",
    "tests/test_original_owner_acl_live.py::test_restricted_original_wait_and_permission_recheck": "reclaim-original-lock",
    "tests/test_original_owner_acl_live.py::test_restricted_original_retention_refuses_claimed_object": "reclaim-original-retention",
}

_WORKSPACE_ASSEMBLY_TARGET = "tests/test_workspace_module_assembly_live.py"
_IDENTITY_ASSEMBLY_TARGET = "tests/test_identity_module_assembly_live.py"
_M2B_BUSINESS_TARGET = "tests/test_m2b_business_live.py"
_REFERENCE_IO_TARGET = "tests/test_reference_saved_io_live.py"
_SAVED_MIGRATION_NODE = (
    "tests/test_saved_io_migration_live.py"
    "::test_registered_copy_and_retained_route_use_exact_live_receipts"
)
_SAVED_SOURCE_FILES = frozenset(
    {
        _REFERENCE_IO_TARGET,
        "tests/test_dataset_import_runtime.py",
        "tests/test_dataset_import_saved_io_live.py",
        "tests/test_saved_outputs_source_live.py",
    }
)
_SAVED_SOURCE_NODES = frozenset(
    {
        "tests/test_saved_source_channel_live.py::test_saved_source_registered_binding_probe_and_denial",
        "tests/test_saved_media_io_live.py::test_registered_media_round_trip_and_denial",
        _SAVED_MIGRATION_NODE,
        "tests/test_training_saved_io_live.py::test_training_attached_archive_publication_and_retirement",
        "tests/test_training_saved_io_live.py::test_training_attached_executor_put_and_finish",
    }
)
_EMBEDDED_LOGIN_FILES = frozenset(
    {
        _IDENTITY_ASSEMBLY_TARGET,
        "tests/test_model_lineage.py",
        "tests/test_sample_evaluations.py",
    }
)
_WORKSPACE_DEFAULT_TARGETS = frozenset(
    {
        _WORKSPACE_ASSEMBLY_TARGET,
        "tests/test_workspace_worker_assembly_live.py",
    }
)
_M2B_DEFAULT_FILES = frozenset(
    {
        _M2B_BUSINESS_TARGET,
        "tests/test_dataset_source_private_live.py",
        "tests/test_guide_clone_api_login_native.py",
        "tests/test_identity_avatar_acl_live.py",
        "tests/test_jobs_ordinary_ledger_live.py",
        "tests/test_learning_selection_live.py",
        "tests/test_m2b_private_access_live.py",
        "tests/test_m2b_runtime_acl_replay.py",
        "tests/test_media_ledger_private_live.py",
        *_SAVED_SOURCE_FILES,
        "tests/test_prediction_runtime_native.py",
        "tests/test_storage_inventory_acl.py",
    }
)
_M2B_DEFAULT_NODES = frozenset(
    {
        *_SAVED_SOURCE_NODES,
        "tests/test_bug_attachment_uploads.py::test_live_diagnostics_login_preserves_report_and_exact_version_attachment_lifecycle",
        "tests/test_identity_module_assembly_live.py::test_complete_compose_remote_api_workers_restart_and_restore",
        "tests/test_identity_module_assembly_live.py::test_embedded_identity_with_isolated_mail_on_cold_api",
        "tests/test_mask_ai_observability.py::test_metrics_reads_current_tracker_and_accept_ledger_as_api_login",
        "tests/test_model_lineage.py::test_real_concurrent_decisions_and_publication_preserve_both_notes",
        "tests/test_notifications_module_assembly_live.py::test_notifications_module_cold_composition_and_full_restore",
        "tests/test_notifications_module_assembly_live.py::test_notifications_hosted_beat_recovers_after_redis_acl_outage",
        "tests/test_sample_evaluation_worker.py::test_cancel_stays_pending_while_native_busy_and_seals_before_late_post",
        "tests/test_sample_evaluation_worker.py::test_dispatch_without_private_jobs_read",
        "tests/test_sample_evaluation_worker.py::test_private_learning_model_and_evaluation_acl",
        "tests/test_storage_fault_states.py::test_real_login_observes_and_reads_without_private_storage_access",
        "tests/test_storage_migration_copy_worker.py::test_real_collector_and_plan_once_with_no_source_or_target_sdk_calls",
        "tests/test_system_settings_patch.py::test_live_settings_cas_rechecks_after_another_admin_commits",
        "tests/test_system_settings_patch.py::test_live_smtp_attempt_uses_only_dedicated_mail_login",
        "tests/test_training_dataset_owner_fences.py::test_api_login_publishes_and_replays_immutable_training_package",
        "tests/test_training_dataset_owner_fences.py::test_finalization_fails_closed_on_locked_snapshot_rows",
        "tests/test_training_executor_handoff.py::test_live_executor_claim_replay_competition_cancel_and_finish_replay",
        "tests/test_training_executor_handoff.py::test_finish_commit_failure_rolls_back_attempt_and_lease_before_retry",
        "tests/test_training_storage_deletion.py::test_cleanup_commit_failure_keeps_retryable_intent",
        "tests/test_video_export_worker.py::test_delivery_capabilities_deny_cross_role_and_table_access",
        "tests/test_video_prepare_lock_windows.py::test_ordinary_video_login_keeps_current_and_historical_receipts_without_table_rights",
    }
)


def _is_m2b_default_target(target: str) -> bool:
    node = target.split("[", 1)[0]
    return node.split("::", 1)[0] in _M2B_DEFAULT_FILES or node in _M2B_DEFAULT_NODES


TEST_FILES = (
    "tests/test_video_frame_service.py",
    "tests/test_video_control_worker_binding.py",
    "tests/test_video_derivative_seeds.py::test_request_ownership_marker_cannot_be_supplied_by_caller",
    "tests/test_video_derivative_seeds.py::test_fixture_video_publication_proof_uses_private_postgres_state",
    "tests/test_jobs_fixture_cleanup_binding.py",
    "tests/test_jobs_fixture_cleanup_native.py",
    "tests/test_jobs_fixture_media_publication_native.py",
    "tests/test_predictions_import.py::test_owned_prediction_import_cleanup",
    "tests/test_fixture_run_ownership.py::test_synchronous_fixture_import_proof",
    "tests/test_fixture_run_ownership.py::test_synchronous_fixture_import_proof_rejects",
    "tests/test_retry_born_dataset_scope.py",
    "tests/test_cross_frame_born_dataset_scope.py",
    "tests/test_task_upload_managed_contract.py",
    "tests/test_inference_pool_reference_native.py",
    "tests/test_labeling_video_control_native.py",
    "tests/test_labeling_annotation_transfer_native.py",
    "tests/test_labeling_core_read_native.py",
    "tests/test_labeling_export_core_locks_native.py",
    "tests/test_image_export_scope_native.py",
    "tests/test_fixture_export_owner_native.py",
    "tests/test_labeling_mask_quality_native.py",
    "tests/test_labeling_spatial_quality_native.py",
    "tests/test_labeling_task_maintenance_native.py",
    "tests/test_audit_maintenance_native.py",
    "tests/test_audit_fixture_cleanup_native.py",
    "tests/test_fixture_audit_migration.py",
    "tests/test_audit_maintenance_module_live.py",
    "tests/test_audit_metadata_delivery_live.py",
    "tests/test_audit_consumers_cold.py",
    "tests/test_audit_maintenance_consumers_cold.py",
    "tests/test_scale_seed_workspace_native.py",
    "tests/test_workspace_e2e_native.py",
    "tests/test_gpu_registry_delete_noop_native.py",
    "tests/test_identity_e2e_replay.py",
    "tests/test_identity_e2e_native.py",
    "tests/test_identity_e2e_live.py",
    "tests/test_identity_fixture_preferences_native.py",
    "tests/test_identity_role_audit_native.py",
    "tests/test_identity_role_audit_live.py",
    "tests/test_identity_seed_selection_live.py",
    "tests/test_identity_seed_selection_native.py",
    "tests/test_image_import_identity_profile.py",
    "tests/test_fixture_auxiliary_retirement.py",
    "tests/test_media_seed_identity_live.py",
    "tests/test_media_seed_identity_native.py",
    "tests/test_seed_parent_operator_live.py",
    "tests/test_seed_parent_operator_native.py",
    "tests/test_workspace_seed_operator_live.py",
    "tests/test_workspace_seed_operator_native.py",
    "tests/test_workspace_import_cli_live.py",
    "tests/test_workspace_worker_assembly_live.py",
    "tests/test_workspace_cross_frame_worker_live.py",
    "tests/test_workspace_video_tracker_worker_live.py",
    "tests/test_workspace_export_worker_live.py",
    _WORKSPACE_ASSEMBLY_TARGET,
    "tests/test_workspace_consumer_profile_native.py",
    "tests/test_video_deletion_retirement_native.py",
    "tests/test_video_tracker_input_retention_roles.py",
    "tests/test_video_deletion_retirement_bindings.py",
    "tests/test_video_deletion_retirement_http_native.py",
    "tests/test_workspace_dataset_unlink_native.py",
    "tests/test_labeling_counter_deltas_native.py",
    "tests/test_labeling_counter_deltas_workflows_native.py",
    "tests/test_workspace_project_facts_legacy.py",
    "tests/test_workspace_project_facts_native.py",
    "tests/test_workspace_project_facts_consumers_native.py",
    "tests/test_workspace_member_facts_legacy.py",
    "tests/test_workspace_member_facts_native.py",
    "tests/test_workspace_actor_scope_legacy.py",
    "tests/test_workspace_actor_scope_native.py",
    "tests/test_project_owner_persistence_native.py",
    "tests/test_labeling_project_counter_owner_native.py",
    "tests/test_workspace_actorless_project_read_native.py",
    "tests/test_workspace_member_locks_native.py",
    "tests/test_workspace_project_get_native.py",
    "tests/test_workspace_project_reference_native.py",
    "tests/test_workspace_project_exclusive_native.py",
    "tests/test_workspace_project_access_unlocked_native.py",
    "tests/test_workspace_project_deletion_native.py",
    "tests/test_workspace_project_maintenance_native.py",
    "tests/test_identity_registration_status_native.py",
    "tests/test_identity_read_relations_native.py",
    "tests/test_identity_accepted_api_key_native.py",
    "tests/test_training_authority_contract.py",
    "tests/test_training_dataset_original_boundaries.py::test_http_training_authority_without_private_identity_select",
    "tests/test_training_dataset_owner_fences.py::test_finalization_fails_closed_on_locked_snapshot_rows",
    "tests/test_inference_gpu_routing_native.py::test_gpu_endpoint_opens_large_native_capture_without_losing_metadata",
    "tests/test_video_restore_native.py",
    "tests/test_video_restore_live.py",
    "tests/test_video_restore_worker_live.py",
    "tests/test_labeling_video_writer_native.py",
    "tests/test_labeling_task_locks_native.py",
    "tests/test_labeling_task_workflow_native.py",
    "tests/test_labeling_annotation_statistics_native.py",
    "tests/test_labeling_scene_track_domain_native.py",
    "tests/test_labeling_annotation_writes_native.py",
    "tests/test_labeling_annotation_bulk_native.py",
    "tests/test_labeling_video_bbox_editing_native.py",
    "tests/test_labeling_batch_control_native.py",
    "tests/test_labeling_runtime_acl_native.py",
    "tests/test_labeling_e2e_fixture_writes_native.py",
    "tests/test_labeling_prediction_records_native.py",
    "tests/test_labeling_scene_track_commands_native.py",
    "tests/test_labeling_cross_frame_writes_native.py",
    "tests/test_labeling_annotation_prediction_native.py",
    "tests/test_labeling_annotation_maintenance_native.py",
    "tests/test_labeling_discussion_native.py",
    "tests/test_labeling_mask_edits_native.py",
    "tests/test_labeling_scene_read_native.py",
    "tests/test_labeling_module_restore.py",
    "tests/test_labeling_module_api_cold.py",
    "tests/test_labeling_video_writer_live.py",
    "tests/test_labeling_video_writer_restore.py",
    "tests/test_video_execution_checkpoints_native.py",
    "tests/test_video_window_journal_native.py",
    "tests/test_video_execution_plan_native_validation.py",
    "tests/test_video_window_recovery_live.py",
    "tests/test_video_window_delivery_live.py",
    "tests/test_video_window_journal.py",
    "tests/test_inference_scope_retirement_native.py",
    "tests/test_video_execution_native.py",
    "tests/test_video_execution_fences.py",
    "tests/test_video_execution_summary.py",
    "tests/test_video_execution_bindings.py",
    "tests/test_inference_scope_retirement_live.py",
    "tests/test_inference_scope_retirement_bindings.py",
    "tests/test_inference_routing_native.py",
    "tests/test_inference_registry_ingress_native.py",
    "tests/test_inference_registry_ingress_postgres.py",
    "tests/test_inference_registry_ingress_selection.py",
    "tests/test_inference_registry_ingress_bindings.py",
    "tests/test_inference_registry_ingress_restore.py",
    "tests/test_inference_registry_ingress_migrations.py",
    "tests/test_inference_health_data_native.py",
    "tests/test_inference_health_data_binding.py",
    "tests/test_inference_health_data_postgres.py",
    "tests/test_inference_backend_probe_native.py",
    "tests/test_inference_backend_probe_bindings.py",
    "tests/test_inference_backend_probe_restore.py",
    "tests/test_inference_backend_dependency_reads_native.py",
    "tests/test_inference_backend_dependency_reads_restore.py",
    "tests/test_workspace_backend_bindings_native.py",
    "tests/test_workspace_backend_bindings_restore.py",
    "tests/test_inference_pipeline_native.py",
    "tests/test_workspace_project_access_step_native.py",
    "tests/test_workspace_project_authority_locking.py",
    "tests/test_workspace_project_access_step_restore.py",
    "tests/test_workspace_class_rename_native.py",
    "tests/test_project_class_rename_owner.py",
    "tests/test_project_class_rename_owner_fences.py",
    "tests/test_labeling_class_rename_effect.py",
    "tests/test_labeling_class_query_native.py",
    "tests/test_workspace_project_access_queries_native.py",
    "tests/test_workspace_project_access_migration.py",
    "tests/test_workspace_project_access_read_phases.py",
    "tests/test_workspace_directory_queries_native.py",
    "tests/test_workspace_member_authority_migration.py",
    "tests/test_workspace_member_authority_temporals.py",
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
    "tests/test_identity_provisioning_native.py",
    "tests/test_workspace_membership_native.py",
    "tests/test_workspace_membership_roles.py",
    "tests/test_workspace_membership_restore.py",
    "tests/test_workspace_owner_transfer_native.py",
    "tests/test_workspace_owner_transfer_roles.py",
    "tests/test_workspace_owner_transfer_restore.py",
    "tests/test_workspace_owner_transfer_namespace_migration.py",
    "tests/test_workspace_owner_transfer_failures.py",
    "tests/test_owner_data_namespace_security.py",
    "tests/test_grant_namespace_security.py",
    "tests/test_workspace_configuration_dependencies_native.py",
    "tests/test_workspace_configuration_dependencies_roles.py",
    "tests/test_workspace_project_query_support_native.py",
    "tests/test_identity_actor_context_native.py",
    "tests/test_identity_account_locks_native.py",
    "tests/test_users_role_matrix.py::TestRoleMatrix::test_sa_cannot_demote_last_super_admin",
    "tests/test_identity_directory_native.py",
    "tests/test_identity_directory_roles.py",
    "tests/test_identity_invitations_native.py",
    "tests/test_identity_invitations_roles.py",
    "tests/test_identity_invitation_gateway.py",
    "tests/test_identity_mail_topology.py",
    "tests/test_identity_deactivation_native.py",
    "tests/test_identity_deactivation_roles.py",
    "tests/test_identity_groups_native.py",
    "tests/test_identity_groups_roles.py",
    "tests/test_identity_challenges_native.py",
    "tests/test_identity_challenges_roles.py",
    "tests/test_identity_credential_mail_native.py",
    "tests/test_identity_recovery_gateway.py",
    "tests/test_identity_accounts_native.py",
    "tests/test_identity_presence_separation.py",
    "tests/test_identity_preferences_separation.py",
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
    "tests/test_jobs_lifecycle_refresh_loader.py",
    "tests/test_jobs_lifecycle_native.py",
    "tests/test_jobs_list_query.py",
    "tests/test_jobs_list_query_native.py",
    "tests/test_async_job_actor_freshness.py",
    "tests/test_notifications.py",
    "tests/test_notifications_module_assembly_live.py",
    "tests/test_notification_terminal_append.py",
    "tests/test_notification_jobs_scope.py",
    "tests/test_notification_inbox_query.py",
    "tests/test_notification_delivery_read.py",
    "tests/test_notification_delivery_read_native.py",
    "tests/test_notification_read_relations_baseline.py",
    "tests/test_notification_delivery_scope.py",
    "tests/test_notification_read_state_cache.py",
    "tests/test_notification_delete_cache.py",
    "tests/test_notifications_delete_baseline.py",
    "tests/test_notification_read_relations.py",
    "tests/test_dataset_import_delivery.py::test_notification_delivery_acks_after_publish_by_persisted_id",
    "tests/test_ws_revalidation.py::test_notification_parser_resolves_stored_row",
    "tests/test_notifications_socket_lifecycle.py",
    "tests/test_notifications_socket_live.py",
    "tests/test_audit_append_adapter.py",
    "tests/test_audit_append_native.py",
    "tests/test_audit_logs.py",
    "tests/test_audit_query_routes.py",
    "tests/test_audit_read_relations_native.py",
    "tests/test_audit_project_scope.py::test_project_admin_audit_scope_and_superadmin_global_access",
    "tests/test_audit_project_scope.py::test_project_admin_audit_relation_preserves_native_inet_dto_values",
    "tests/test_audit_metadata_append.py",
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
    "tests/test_dataset_source_ingest_authority_runtime.py",
    "tests/test_dataset_source_ingest_delivery.py",
    "tests/test_dataset_import.py",
    "tests/test_dataset_import_native.py",
    "tests/test_dataset_import_migration.py",
    "tests/test_assets_import_bindings.py",
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
    "tests/test_dataset_source_auxiliary_references.py",
    "tests/test_dataset_source_support.py",
    "tests/test_dataset_upload_data.py",
    "tests/test_dataset_upload_native.py",
    "tests/test_dataset_upload_storage.py",
    "tests/test_dataset_upload_s3_runtime.py",
    "tests/test_upload_retry_migration.py",
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
    "tests/test_native_pyramid_saved_targets.py",
    "tests/test_native_pyramid_lifecycle_live.py",
    "tests/test_image_admission_share.py",
    "tests/test_image_thumbnail_publication.py",
    "tests/test_image_pyramid.py",
    "tests/test_image_backfill_maintenance.py",
    "tests/test_image_worker_compatibility.py",
    "tests/test_media_celery_loops.py",
    "tests/test_image_pyramid_http.py",
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
    "tests/test_fixture_native_media_admissions.py::test_legacy_native_reader_origin_is_not_reused_until_retired",
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
    *_RECLAMATION_PROFILES,
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
        "--retain-failed-runtime",
        action="store_true",
        help="retain the outer database, storage identities and evidence after failure; test-managed processes may still stop",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--test-file",
        dest="test_files",
        action="append",
        type=_test_file,
        metavar="TEST_NODEID",
        help="select an apps/api test file or node ID; repeat to replace the default suite",
    )
    selection.add_argument(
        "--reclamation-only",
        action="store_true",
        help="run each managed-original reclamation profile on its own disposable database",
    )
    parser.add_argument(
        "--workspace-consumer-profile",
        choices=("strict", "default"),
        default="strict",
        help="Workspace consumer credentials: strict function-only or default LAN capabilities",
    )
    args = parser.parse_args(argv)
    targets = args.test_files or ()
    if args.workspace_consumer_profile == "default":
        workspace_assembly = (
            len(targets) == 1
            and targets[0].split("::", 1)[0] in _WORKSPACE_DEFAULT_TARGETS
        )
        if not workspace_assembly and not (
            targets and all(_is_m2b_default_target(target) for target in targets)
        ):
            parser.error(
                "The default profile requires one Workspace assembly target "
                "or explicitly approved M2b targets"
            )
    elif any(
        _is_m2b_default_target(target) and target.partition("[")[0] not in TEST_FILES
        for target in targets
    ):
        parser.error("M2b targets require --workspace-consumer-profile default")
    return args


def _write_evidence(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _retire_resource_root(resource_root: Path, cleanup: dict) -> None:
    """Keep physical store evidence after the owned runtime itself is retired."""
    from worktree_env import state_path

    directory = state_path(resource_root, "test")
    receipts = sorted(directory.glob("fixture-original-receipt-*.json"))
    journals = sorted(directory.glob("fixture-store-owner-*.json"))
    legacy_journal = state_path(
        resource_root, "test", "legacy-original-retirement.json"
    )
    if legacy_journal.exists():
        journals.append(legacy_journal)
    if receipts or journals:
        cleanup["fixture_evidence"] = [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in receipts + journals
        ]
        cleanup["resource_root_retained"] = str(resource_root)
        cleanup["resource_root_retention_reason"] = "fixture_evidence"
        return
    shutil.rmtree(resource_root)


_STRICT_PROFILE_TARGETS = {
    "tests/test_image_import_identity_profile.py": "identity-image-import",
    "tests/test_fixture_auxiliary_retirement.py": "fixture-auxiliary-retirement",
    "tests/test_fixture_audit_migration.py": "fixture-audit-migration",
    "tests/test_audit_maintenance_native.py": "audit-maintenance-native",
    "tests/test_audit_maintenance_module_live.py": "audit-maintenance-module",
    "tests/test_audit_consumers_cold.py": "audit-consumers-cold",
    "tests/test_audit_maintenance_consumers_cold.py": "audit-maintenance-consumers-cold",
    "tests/test_seed_parent_operator_native.py": "parent-seed-native",
    "tests/test_identity_role_audit_live.py": "identity-role-audit",
    "tests/test_identity_e2e_live.py": "identity-e2e",
    "tests/test_identity_seed_selection_live.py": "scale-seed",
    "tests/test_notifications_module_assembly_live.py": "notifications-restore",
    "tests/test_media_seed_identity_live.py": "media-seed",
    "tests/test_seed_parent_operator_live.py": "parent-seed",
    _WORKSPACE_ASSEMBLY_TARGET: "workspace-assembly",
    "tests/test_workspace_seed_operator_live.py": "workspace-seed",
    "tests/test_workspace_import_cli_live.py": "workspace-import",
    "tests/test_workspace_video_tracker_worker_live.py": "workspace-video-tracker",
    "tests/test_video_deletion_retirement_http_native.py": "video-deletion-http",
    "tests/test_prediction_retry_legacy_migration.py": "pre-retry-migration",
    "tests/test_video_tracker_input_lifecycle.py": "video-input-lanes",
    "tests/test_mask_gc_native.py": "mask-gc-fences",
    "tests/test_mask_gc_protocol_native.py": "mask-gc-fences",
    "tests/test_mask_gc_live.py": "mask-gc-fences",
    "tests/test_native_pyramid_lifecycle_live.py": "native-lifecycle",
    "tests/test_mask_gc_collection_live.py": "mask-gc-collection",
    "tests/test_mask_gc_runtime_image.py": "mask-gc-runtime",
    **_RECLAMATION_PROFILES,
}


def _append_profile_junit(
    suites: ET.Element,
    collection: Path,
    junit: Path,
    targets: tuple[str, ...],
    expected: list[str],
) -> None:
    actual = json.loads(collection.read_text(encoding="utf-8"))
    if (
        not isinstance(actual, list)
        or any(not isinstance(node, str) for node in actual)
        or Counter(actual) != Counter(expected)
    ):
        raise RuntimeError("Acceptance profile collection differs from the default")
    _passed_test_count(junit, targets, actual)
    root = ET.parse(junit).getroot()
    for suite in [root] if root.tag == "testsuite" else root.findall("testsuite"):
        suites.append(suite)


def _run_strict_default(args: argparse.Namespace, selected: tuple[str, ...]) -> int:
    """Reconcile one default collection across isolated existing verifier profiles."""
    output = args.output.resolve()
    junit = (
        args.junit_xml.resolve()
        if args.junit_xml
        else output.with_name(f"{output.stem}.junit.xml")
    )
    manifest = output.with_name(f"{output.stem}.collection.json")
    if len({output, junit, manifest}) != 3:
        raise SystemExit(
            "--output, --junit-xml and the collection manifest must be different paths"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    junit.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    junit.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    directory = Path(
        tempfile.mkdtemp(prefix=f"{output.stem}-profiles-", dir=output.parent)
    ).resolve()
    groups = {"ordinary": []}
    for name in _STRICT_PROFILE_TARGETS.values():
        groups[name] = []
    for target in selected:
        path = target.split("::", 1)[0]
        name = _STRICT_PROFILE_TARGETS.get(
            target, _STRICT_PROFILE_TARGETS.get(path, "ordinary")
        )
        groups[name].append(target)
    groups = {name: targets for name, targets in groups.items() if targets}
    evidence = {
        "schema": 1,
        "acceptance": "module-data-boundary",
        "scope": (
            "managed-original-reclamation"
            if args.reclamation_only
            else "strict-default-isolated-profiles"
        ),
        "status": "running",
        "phase": "default-collection",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_root": str(ROOT),
        "test_files": list(selected),
        "collection_manifest": str(manifest),
        "junit_xml": str(junit),
        "profile_directory": str(directory),
        "collection_evidence": str(directory / "collection.json"),
        "profiles": {
            name: {"status": "not_run", "evidence": str(directory / f"{name}.json")}
            for name in groups
        },
    }
    _write_evidence(output, evidence)
    exit_code = 1
    diagnostic_arguments = (
        ["--retain-failed-runtime"] if args.retain_failed_runtime else []
    )
    try:
        collection_output = directory / "collection.json"
        collection_args = ["--output", str(collection_output), *diagnostic_arguments]
        if args.reclamation_only:
            collection_args.append("--reclamation-only")
        if (
            main(
                collection_args,
                collect_only=True,
            )
            != 0
        ):
            raise RuntimeError("Acceptance default collection stage failed")
        collected = json.loads(
            collection_output.with_name("collection.collection.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            not isinstance(collected, list)
            or not collected
            or any(not isinstance(node, str) for node in collected)
        ):
            raise RuntimeError("Acceptance default collection is empty or invalid")
        manifest.write_text(
            json.dumps(collected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        evidence["collected_test_nodes"] = collected
        if len(set(collected)) != len(collected):
            raise RuntimeError(
                "Acceptance default collection contains duplicate node IDs"
            )
        target_groups = {
            target: name for name, targets in groups.items() for target in targets
        }
        expected = {name: [] for name in groups}
        for node in collected:
            name = target_groups.get(node)
            selector = node.partition("[")[0]
            while name is None and selector:
                name = target_groups.get(selector)
                selector = selector.rpartition("::")[0]
            if name is None:
                raise RuntimeError("Acceptance collection contains an unselected file")
            expected[name].append(node)
        evidence["collection_status"] = "collected"
        suites = ET.Element("testsuites")
        complete = True
        for name, targets in groups.items():
            entry = evidence["profiles"][name]
            entry["status"] = "running"
            evidence["phase"] = f"profile-{name}"
            _write_evidence(output, evidence)
            profile_output = directory / f"{name}.json"
            arguments = ["--output", str(profile_output), *diagnostic_arguments]
            for target in targets:
                arguments.extend(("--test-file", target))
            result = main(arguments, partition_default=False)
            report = json.loads(profile_output.read_text(encoding="utf-8"))
            entry["status"] = report["status"]
            entry["test_exit_code"] = report["test_exit_code"]
            entry["cleanup"] = report["cleanup"]
            profile_manifest = profile_output.with_name(f"{name}.collection.json")
            profile_junit = profile_output.with_name(f"{name}.junit.xml")
            entry["collection_matches_default"] = None
            entry["junit_reconciled"] = None
            if profile_manifest.is_file():
                try:
                    actual = json.loads(profile_manifest.read_text(encoding="utf-8"))
                    entry["collection_matches_default"] = (
                        isinstance(actual, list)
                        and all(isinstance(node, str) for node in actual)
                        and Counter(actual) == Counter(expected[name])
                    )
                except (OSError, ValueError) as error:
                    entry["collection_error"] = type(error).__name__
            if result == 0 and entry["collection_matches_default"] is True:
                try:
                    _append_profile_junit(
                        suites,
                        profile_manifest,
                        profile_junit,
                        tuple(targets),
                        expected[name],
                    )
                    entry["junit_reconciled"] = True
                except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
                    entry["junit_reconciled"] = False
                    entry["reconciliation_error"] = type(error).__name__
            if result != 0 or entry["junit_reconciled"] is not True:
                entry["status"] = "failed"
                complete = False
            _write_evidence(output, evidence)
            if report["cleanup"]["errors"]:
                evidence["failure"] = {
                    "phase": f"profile-{name}-cleanup",
                    "error_type": "CleanupError",
                }
                break
            if result != 0 and report["test_exit_code"] is None:
                evidence["failure"] = {
                    "phase": f"profile-{name}-setup",
                    "error_type": "ProfileSetupFailure",
                }
                break
        if complete and all(
            entry["status"] == "passed" for entry in evidence["profiles"].values()
        ):
            ET.ElementTree(suites).write(junit, encoding="utf-8", xml_declaration=True)
            evidence["passed_test_count"] = _passed_test_count(
                junit, selected, collected
            )
            evidence["status"] = "passed"
            exit_code = 0
        elif "failure" not in evidence:
            evidence["failure"] = {
                "phase": "profile-reconciliation",
                "error_type": "ProfileFailure",
            }
    except Exception as error:
        evidence["failure"] = {
            "phase": "default-reconciliation",
            "error_type": type(error).__name__,
        }
        if isinstance(error, RuntimeError) and str(error).startswith("Acceptance "):
            evidence["failure"]["diagnostic"] = str(error)
    except BaseException as error:
        evidence["failure"] = {
            "phase": "default-reconciliation",
            "error_type": type(error).__name__,
        }
        raise
    finally:
        if exit_code != 0:
            evidence["status"] = "failed"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_evidence(output, evidence)
        print(
            f"{'PASS' if exit_code == 0 else 'FAIL'}: module-data strict default; "
            f"evidence={output} junit={junit}",
            flush=True,
        )
    return exit_code


def _passed_test_count(
    junit: Path, selected: tuple[str, ...], collected: list[str]
) -> int:
    root = ET.parse(junit).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise RuntimeError("Invalid acceptance JUnit report")
    suites = ([root] if root.tag == "testsuite" else []) + root.findall(".//testsuite")
    for suite in suites:
        for state in ("failures", "errors", "skipped"):
            if int(suite.get(state, "0")) != 0:
                raise RuntimeError(f"Acceptance JUnit reports {state}")
        if any(
            suite.find(state) is not None for state in ("failure", "error", "skipped")
        ):
            raise RuntimeError("Acceptance JUnit reports a suite-level failure")
        if "tests" in suite.attrib and int(suite.attrib["tests"]) != len(
            suite.findall(".//testcase")
        ):
            raise RuntimeError("Acceptance JUnit test count is incomplete")

    cases = root.findall(".//testcase")
    if not cases:
        raise RuntimeError("Acceptance ran no test cases")
    for case in cases:
        if not case.get("classname") or not case.get("name"):
            raise RuntimeError("Acceptance JUnit has an unidentified test case")
        if any(
            case.find(state) is not None for state in ("skipped", "failure", "error")
        ):
            raise RuntimeError("Acceptance JUnit has a non-passing test case")

    def matches(target: str, case: ET.Element) -> bool:
        selector, bracket, parameter = target.partition("[")
        path, *nodes = selector.split("::")
        if bracket and nodes:
            nodes[-1] += bracket + parameter
        module = path.removesuffix(".py").replace("/", ".")
        classname = case.get("classname", "")
        name = case.get("name", "")
        if not nodes:
            return classname == module or classname.startswith(module + ".")
        class_name = module + "." + ".".join(nodes)
        if classname == class_name or classname.startswith(class_name + "."):
            return True
        expected_class = ".".join([module, *nodes[:-1]])
        expected_name = nodes[-1]
        return classname == expected_class and (
            name == expected_name
            or ("[" not in expected_name and name.startswith(expected_name + "["))
        )

    if (
        not isinstance(collected, list)
        or not collected
        or any(not isinstance(node, str) for node in collected)
    ):
        raise RuntimeError("Acceptance collection manifest is empty or invalid")
    expected = Counter()
    for node in collected:
        selector, bracket, parameter = node.partition("[")
        path, *parts = selector.split("::")
        if not parts:
            raise RuntimeError("Acceptance collection manifest has an invalid node ID")
        expected_name = parts[-1] + (bracket + parameter if bracket else "")
        expected_class = ".".join(
            [path.removesuffix(".py").replace("/", "."), *parts[:-1]]
        )
        expected[(expected_class, expected_name)] += 1
    reported = Counter((case.get("classname"), case.get("name")) for case in cases)
    if expected != reported:
        missing_cases = list((expected - reported).elements())
        extra_cases = list((reported - expected).elements())
        raise RuntimeError(
            f"Acceptance JUnit differs from collection: missing={missing_cases}, extra={extra_cases}"
        )
    missing = [
        target
        for target in selected
        if not any(matches(target, case) for case in cases)
    ]
    if missing:
        raise RuntimeError(
            f"Acceptance JUnit is missing selected targets: {', '.join(missing)}"
        )
    return len(cases)


def _node_failure_diagnostic(
    stage: str, result: subprocess.CompletedProcess
) -> dict[str, object]:
    stderr = result.stderr or ""
    diagnostic: dict[str, object] = {
        "stage": stage,
        "returncode": result.returncode,
        "category": "node_failure",
    }
    module_match = re.search(r"Cannot find package '([^']+)'", stderr)
    if module_match:
        diagnostic["category"] = "missing_node_module"
        module_name = module_match.group(1)
        if re.fullmatch(r"[A-Za-z0-9@._/-]+", module_name):
            diagnostic["module"] = module_name
        return diagnostic
    error_class_match = re.search(
        r"(?m)^(AssertionError|TypeError|RangeError|ReferenceError|SyntaxError|Error)"
        r"(?:\s+\[[A-Z0-9_]+\])?:",
        stderr,
    )
    if error_class_match:
        diagnostic["error_class"] = error_class_match.group(1)
    sqlstate_match = re.search(r"\bcode:\s*['\"]([0-9A-Z]{5})['\"]", stderr)
    if sqlstate_match:
        diagnostic["category"] = "postgresql_error"
        diagnostic["sqlstate"] = sqlstate_match.group(1)
    elif "ECONNREFUSED" in stderr or "ETIMEDOUT" in stderr:
        diagnostic["category"] = "connection_error"
    elif "AssertionError" in stderr:
        diagnostic["category"] = "assertion_failure"
    elif "ERR_ASSERTION" in stderr:
        diagnostic["category"] = "assertion_failure"
    return diagnostic


def _cleanup_storage_scope(backend) -> dict:
    from worktree_resources import docker, verified_shared_bootstrap_minio

    scheme = "https" if backend.settings.minio_use_ssl else "http"
    endpoint_url = f"{scheme}://{backend.settings.minio_endpoint}"
    endpoint = urlsplit(endpoint_url)
    shared = verified_shared_bootstrap_minio(endpoint_url)
    matches = []
    if shared is not None:
        matches.append(shared)
    else:
        if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("The local MinIO endpoint must be loopback-only")
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
        tag["Key"] == "yoao-worktree-owner"
        and tag["Value"] == backend.resources["owner"]
        for tag in tags
    ):
        raise RuntimeError("The cleanup bucket is not owned by this acceptance")
    inputs_bucket = backend.resources["buckets"]["MINIO_INPUTS_BUCKET"]
    input_tags = backend.s3.get_bucket_tagging(Bucket=inputs_bucket)["TagSet"]
    if not any(
        tag["Key"] == "yoao-worktree-owner"
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
            retries={"total_max_attempts": 1},
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
        tag["Key"] == "yoao-worktree-owner"
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


async def _retire_notification_preparation(
    backend,
    birth_oid,
    user_id,
    project_id,
    member_id,
    notification_ids,
    *,
    require_empty,
):
    """Retire only this verifier's completed cross-module probes."""
    import asyncpg

    from worktree_env import read_state, state_path

    resources = backend.resources
    manifest = read_state(state_path(Path(resources["root"]), "test", "resources.json"))
    if (
        manifest != backend.manifest
        or manifest.get("postgres", {}).get("database_oid") != birth_oid
        or type(birth_oid) is not int
        or birth_oid <= 0
        or backend.url.username != "owner"
        or backend.url.database != resources["database"]
    ):
        raise RuntimeError("Notifications preparation birth identity is unproven")
    user_id, project_id, member_id = map(UUID, (user_id, project_id, member_id))
    notification_ids = [UUID(value) for value in notification_ids]
    connection = await asyncpg.connect(
        backend.url.set(drivername="postgresql").render_as_string(hide_password=False),
        timeout=5,
    )
    try:
        async with connection.transaction():
            identity = await connection.fetchrow(
                "SELECT d.oid::bigint, d.datname, "
                "pg_catalog.pg_get_userbyid(d.datdba), "
                "pg_catalog.shobj_description(d.oid,'pg_database'), "
                "current_user, session_user FROM pg_catalog.pg_database d "
                "WHERE d.datname=current_database()"
            )
            if tuple(identity) != (
                birth_oid,
                resources["database"],
                "owner",
                resources["owner"],
                "owner",
                "owner",
            ):
                raise RuntimeError(
                    "Notifications preparation database identity changed"
                )
            await connection.execute("SET LOCAL lock_timeout = '5s'")
            await connection.execute(
                "LOCK TABLE public.users, public.projects, public.project_members, "
                "public.notifications, public.notification_delivery_outbox, "
                "public.notification_preferences IN ACCESS EXCLUSIVE MODE"
            )
            if await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity "
                "WHERE datid=(SELECT oid FROM pg_catalog.pg_database "
                "WHERE datname=current_database()) "
                "AND backend_type='client backend' AND pid<>pg_backend_pid())"
            ):
                raise RuntimeError(
                    "Notifications preparation has other database clients"
                )
            user = await connection.fetchrow(
                "SELECT email,name,password_hash,role,is_active FROM public.users "
                "WHERE id=$1",
                user_id,
            )
            project = await connection.fetchrow(
                "SELECT owner_id,name FROM public.projects WHERE id=$1", project_id
            )
            member = await connection.fetchrow(
                "SELECT project_id,user_id,role,assigned_by FROM public.project_members "
                "WHERE id=$1",
                member_id,
            )
            notifications = await connection.fetch(
                "SELECT id FROM public.notifications WHERE user_id=$1", user_id
            )
            if (
                user is None
                or tuple(user)
                != (
                    f"notification-preference-{user_id}@test.invalid",
                    "Preference owner fixture",
                    "module-data-fixture",
                    "employee",
                    True,
                )
                or project is None
                or tuple(project) != (user_id, "Delivery read Node fixture")
                or member is None
                or tuple(member) != (project_id, user_id, "reviewer", user_id)
                or {row["id"] for row in notifications} != set(notification_ids)
            ):
                raise RuntimeError("Notifications preparation probe rows changed")
            preferences = await connection.fetch(
                "SELECT type,channels FROM public.notification_preferences "
                "WHERE user_id=$1",
                user_id,
            )
            if (
                len(preferences) != 1
                or preferences[0]["type"] != "task.rejected"
                or json.loads(preferences[0]["channels"])
                != {"in_app": False, "email": False, "toast": False}
            ):
                raise RuntimeError("Notifications preparation probe preference changed")
            outbox = await connection.fetchrow(
                """
SELECT count(*) FILTER (WHERE notice.target_type='async_job') AS expected,
       count(delivery.notification_id) AS actual,
       count(*) FILTER (
           WHERE notice.target_type='async_job'
             AND delivery.operation_id=notice.target_id
             AND delivery.recipient_id=notice.user_id
             AND delivery.notification_type=notice.type
             AND delivery.state='pending' AND delivery.attempt=0
             AND delivery.worker_id IS NULL AND delivery.delivered_at IS NULL
       ) AS untouched
FROM public.notifications AS notice
LEFT JOIN public.notification_delivery_outbox AS delivery
  ON delivery.notification_id=notice.id
WHERE notice.id=ANY($1::uuid[])
""",
                notification_ids,
            )
            if not outbox["expected"] or not (
                outbox["expected"] == outbox["actual"] == outbox["untouched"]
            ):
                raise RuntimeError("Notifications preparation probe outbox changed")
            parent_ids = {
                "users": [user_id],
                "projects": [project_id],
                "project_members": [member_id],
                "notifications": notification_ids,
            }
            account_children = {
                ("public", "identity_account_presence", "user_id"),
                ("public", "identity_account_preferences", "user_id"),
                ("public", "notification_preferences", "user_id"),
            }
            references = await connection.fetch(
                "SELECT ns.nspname, child.relname, a.attname, parent.relname AS parent, "
                "pg_catalog.format('%I.%I', ns.nspname, child.relname) AS relation, "
                "pg_catalog.quote_ident(a.attname) AS column_name, "
                "array_length(fk.conkey,1) AS child_columns, "
                "array_length(fk.confkey,1) AS parent_columns, key.attname AS parent_column "
                "FROM pg_catalog.pg_constraint fk "
                "JOIN pg_catalog.pg_class child ON child.oid=fk.conrelid "
                "JOIN pg_catalog.pg_class parent ON parent.oid=fk.confrelid "
                "JOIN pg_catalog.pg_namespace ns ON ns.oid=child.relnamespace "
                "JOIN pg_catalog.pg_attribute a ON a.attrelid=child.oid AND a.attnum=fk.conkey[1] "
                "JOIN pg_catalog.pg_attribute key ON key.attrelid=parent.oid AND key.attnum=fk.confkey[1] "
                "WHERE fk.contype='f' AND fk.confdeltype IN ('c','n','d') "
                "AND fk.confrelid IN ('public.users'::regclass,'public.projects'::regclass, "
                "'public.project_members'::regclass,'public.notifications'::regclass)"
            )
            for ref in references:
                if (
                    ref["child_columns"] != 1
                    or ref["parent_columns"] != 1
                    or ref["parent_column"] != "id"
                ):
                    raise RuntimeError(
                        "Notifications preparation has an unknown cascading reference"
                    )
                child = (ref["nspname"], ref["relname"], ref["attname"])
                if ref["parent"] == "users" and child in account_children:
                    continue
                if ref["parent"] == "notifications" and child == (
                    "public",
                    "notification_delivery_outbox",
                    "notification_id",
                ):
                    continue
                owned_children = (
                    parent_ids.get(ref["relname"], [])
                    if ref["nspname"] == "public"
                    and ref["relname"] in {"notifications", "project_members"}
                    and ref["parent"] in {"users", "projects"}
                    else []
                )
                if await connection.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM {ref['relation']} "
                    f"WHERE {ref['column_name']}=ANY($1::uuid[])"
                    + (" AND id<>ALL($2::uuid[])" if owned_children else "")
                    + ")",
                    parent_ids[ref["parent"]],
                    *([owned_children] if owned_children else []),
                ):
                    raise RuntimeError(
                        "Notifications preparation has an unexpected dependent"
                    )
            if (
                await connection.execute(
                    "DELETE FROM public.notifications WHERE user_id=$1 "
                    "AND id=ANY($2::uuid[])",
                    user_id,
                    notification_ids,
                )
                != f"DELETE {len(notification_ids)}"
                or await connection.execute(
                    "DELETE FROM public.project_members WHERE id=$1", member_id
                )
                != "DELETE 1"
            ):
                raise RuntimeError("Notifications preparation child retirement failed")
            if (
                await connection.execute(
                    "DELETE FROM public.projects WHERE id=$1 AND owner_id=$2",
                    project_id,
                    user_id,
                )
                != "DELETE 1"
            ):
                raise RuntimeError(
                    "Notifications preparation project retirement failed"
                )
            if (
                await connection.execute(
                    "DELETE FROM public.users WHERE id=$1", user_id
                )
                != "DELETE 1"
            ):
                raise RuntimeError("Notifications preparation user retirement failed")
            if require_empty and await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM public.users) "
                "OR EXISTS (SELECT 1 FROM public.projects) "
                "OR EXISTS (SELECT 1 FROM public.datasets)"
            ):
                raise RuntimeError("Image-import preparation is not empty")
    finally:
        await connection.close()
    return {
        "database_oid": birth_oid,
        "database": resources["database"],
        "database_owner": "owner",
        "owner_marker": resources["owner"],
        "user_id": str(user_id),
        "project_id": str(project_id),
        "member_id": str(member_id),
        "notification_ids": [str(value) for value in notification_ids],
        "pending_terminal_deliveries_retired": outbox["untouched"],
        "completed_probe_retired": True,
        "other_clients_absent": True,
        **({"account_project_dataset_empty": True} if require_empty else {}),
    }


async def _fence_image_import_legacy_storage(backend) -> dict:
    """Fence the exact legacy target and inspect its durable physical ledger."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.services import storage_access, storage_maintenance
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.storage_usage_accounting import scope_key
    from worktree_resources import require_owner

    resources = backend.resources
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    if not require_owner(bucket, backend.bucket_info(bucket), resources["owner"]):
        raise RuntimeError("Image-import bucket ownership is unproven")
    if backend.s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise RuntimeError("Image-import bucket versioning changed")
    endpoint = (
        f"{'https' if backend.settings.minio_use_ssl else 'http'}://"
        f"{backend.settings.minio_endpoint}"
    ).rstrip("/")
    identity = managed_storage_identity(endpoint, bucket)
    usage_scope = scope_key(endpoint, bucket)
    engine = create_async_engine(backend.url, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            database = (
                await db.execute(
                    text(
                        "SELECT d.oid,d.datname,"
                        "pg_catalog.shobj_description(d.oid,'pg_database'),current_user "
                        "FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
                    )
                )
            ).one()
            if not (
                database[1] == resources["database"] == backend.url.database
                and database[2] == resources["owner"]
                and database[3] == backend.url.username
            ):
                raise RuntimeError("Image-import database ownership is unproven")
            await db.rollback()
            await storage_access.ensure(db, LEGACY_LOCATION_ID)
            async with db.begin():
                location = await db.scalar(
                    select(StorageLocation)
                    .where(StorageLocation.id == LEGACY_LOCATION_ID)
                    .with_for_update(nowait=True)
                )
                binding = await db.scalar(
                    select(StorageBinding)
                    .where(StorageBinding.purpose == "datasets")
                    .with_for_update(nowait=True)
                )
                if not (
                    location is not None
                    and location.enabled
                    and location.kind == "s3"
                    and location.connection_id is None
                    and location.config_revision == 1
                    and location.config
                    == {
                        "endpoint": backend.settings.minio_endpoint,
                        "use_ssl": backend.settings.minio_use_ssl,
                    }
                    and binding is not None
                    and binding.location_id == LEGACY_LOCATION_ID
                    and binding.generation == 1
                    and binding.bucket == bucket
                ):
                    raise RuntimeError("Image-import legacy storage identity changed")
                gate = await storage_maintenance.lock_gate(db, LEGACY_LOCATION_ID)
                if gate is None or gate.state != "active":
                    raise RuntimeError("Image-import admission gate is not active")
                gate.state = storage_maintenance.TRANSITIONS[("active", "drain")]
                gate.revision += 1
                gate.changed_at = await db.scalar(text("SELECT statement_timestamp()"))
                gate.changed_by = None
                revision = gate.revision
            async with db.begin():
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND backend_type='client backend')"
                    )
                ):
                    raise RuntimeError("Image-import database sessions remain")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.storage_operations "
                        "WHERE location_id=:location)"
                    ),
                    {"location": LEGACY_LOCATION_ID},
                ):
                    raise RuntimeError("Legacy storage operations remain unsettled")
                rows = (
                    (
                        await db.execute(
                            text(
                                "SELECT id,bucket,storage_identity,status,error_code,"
                                "prepared,receipt,write_started_at,settled_at,object_key,version_id "
                                "FROM public.managed_original_creations "
                                "WHERE location_id=:location ORDER BY id"
                            ),
                            {"location": LEGACY_LOCATION_ID},
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    verified = (
                        row["status"] == "verified"
                        and row["bucket"] == bucket
                        and row["storage_identity"] == identity
                        and row["settled_at"] is not None
                        and row["object_key"] not in (None, "")
                        and row["version_id"] not in (None, "", "null")
                    )
                    refused_before_io = (
                        row["status"] == "failed"
                        and row["error_code"] == "storage_unavailable"
                        and row["prepared"] is None
                        and row["receipt"] is None
                        and row["write_started_at"] is None
                    )
                    if not (verified or refused_before_io):
                        raise RuntimeError("Legacy creation outcome is unproven")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.storage_usage_mutations "
                        "WHERE scope_key=:scope AND state IN ('pending','unknown'))"
                    ),
                    {"scope": usage_scope},
                ):
                    raise RuntimeError("Legacy storage accounting is unsettled")
        for page in backend.s3.get_paginator("list_multipart_uploads").paginate(
            Bucket=bucket
        ):
            if page.get("Uploads"):
                raise RuntimeError("Image-import multipart uploads remain")
        if not require_owner(bucket, backend.bucket_info(bucket), resources["owner"]):
            raise RuntimeError("Image-import bucket ownership changed")
        return {
            "database_oid": database[0],
            "location_id": str(LEGACY_LOCATION_ID),
            "storage_identity": identity,
            "usage_scope": usage_scope,
            "gate_revision": revision,
            "creations": len(rows),
            "verified_creations": [
                {
                    "id": str(row["id"]),
                    "key": row["object_key"],
                    "version": row["version_id"],
                }
                for row in rows
                if row["status"] == "verified"
            ],
        }
    finally:
        await engine.dispose()


async def _probe_image_import_guard(backend, scope) -> dict:
    """Prove a real open permit fences retirement without touching its versions."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.models.storage_maintenance import StorageOperation
    from app.services import storage_access, storage_maintenance
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from worktree_env import atomic_json

    bucket = backend.resources["buckets"]["MINIO_DATASETS_BUCKET"]

    def versions():
        return {
            (item["Key"], item["VersionId"])
            for page in backend.s3.get_paginator("list_object_versions").paginate(
                Bucket=bucket
            )
            for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
        }

    before = versions()
    journal, path = scope["journal"], scope["journal_path"]
    journal["phase"] = "guard_probe_admitting"
    atomic_json(path, journal)
    engine = create_async_engine(backend.url, poolclass=NullPool, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            permit = await storage_access.acquire(
                db, LEGACY_LOCATION_ID, "image_import_retirement_probe"
            )
        journal["probe_operation"] = str(permit.id)
        journal["phase"] = "guard_probe_open"
        atomic_json(path, journal)
        try:
            await _fence_image_import_legacy_storage(backend)
        except RuntimeError as error:
            if str(error) != "Legacy storage operations remain unsettled":
                raise
        else:
            raise RuntimeError("An open legacy permit failed to block retirement")
        async with factory() as db:
            operation = await db.get(StorageOperation, permit.id)
            if (
                operation is None
                or operation.location_id != LEGACY_LOCATION_ID
                or operation.revision != permit.revision
                or versions() != before
            ):
                raise RuntimeError("Open-permit guard changed owned storage")
        await permit.__aexit__(None, None, None)
        async with factory.begin() as db:
            gate = await storage_maintenance.lock_gate(db, LEGACY_LOCATION_ID)
            if (
                gate is None
                or gate.state != "draining"
                or await db.scalar(
                    select(StorageOperation.id)
                    .where(StorageOperation.location_id == LEGACY_LOCATION_ID)
                    .limit(1)
                )
                is not None
            ):
                raise RuntimeError("Probe settlement or gate identity changed")
            from app.services.managed_original_reclaim_fence import require_resumable
            from app.services.storage_migration_cutovers import require_target_resumable

            await require_resumable(db, LEGACY_LOCATION_ID)
            await require_target_resumable(db, LEGACY_LOCATION_ID)
            gate.state = storage_maintenance.TRANSITIONS[("draining", "resume")]
            gate.revision += 1
            gate.changed_at = await db.scalar(text("SELECT statement_timestamp()"))
            gate.changed_by = None
        journal["phase"] = "identity_ready"
        journal["guard_probe"] = {
            "operation_id": str(permit.id),
            "blocked": True,
            "versions_unchanged": True,
        }
        atomic_json(path, journal)
        return journal["guard_probe"]
    finally:
        await engine.dispose()


async def _retire_image_import_bucket(backend, prepared, scope, proof) -> None:
    """Retire only this fenced bucket while its database ledger is still locked."""
    from botocore.exceptions import ClientError
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.db.models.storage_maintenance import StorageMaintenanceGate
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.storage_usage_accounting import scope_key
    from worktree_env import atomic_json, read_state
    from worktree_resources import require_owner

    backend._require_destroy_preparation(prepared, "prepared")
    journal = scope["journal"]
    path = scope["journal_path"]
    bucket = backend.resources["buckets"]["MINIO_DATASETS_BUCKET"]
    endpoint = (
        f"{'https' if backend.settings.minio_use_ssl else 'http'}://"
        f"{backend.settings.minio_endpoint}"
    ).rstrip("/")
    if (
        read_state(path) != journal
        or journal.get("phase") not in {"identity_disabled", "retiring_versions"}
        or (
            journal.get("phase") == "identity_disabled"
            and journal.get("partial_probe", {}).get("state") != "canary_created"
        )
        or (
            journal.get("phase") == "retiring_versions"
            and (
                "pending" not in journal
                or journal["pending"] is not None
                or journal.get("partial_probe", {}).get("state") != "retired"
            )
        )
        or journal.get("resources") != backend.resources
        or journal.get("bucket") != bucket
        or journal.get("fence") != proof
        or proof.get("storage_identity") != managed_storage_identity(endpoint, bucket)
        or proof.get("usage_scope") != scope_key(endpoint, bucket)
    ):
        raise RuntimeError("Image-import retirement journal changed")
    engine = create_async_engine(backend.url, poolclass=NullPool, hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            async with db.begin():
                database = (
                    await db.execute(
                        text(
                            "SELECT d.oid,d.datname,"
                            "pg_catalog.shobj_description(d.oid,'pg_database'),current_user "
                            "FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
                        )
                    )
                ).one()
                if not (
                    database[0] == proof["database_oid"]
                    and database[1] == backend.resources["database"]
                    and database[2] == backend.resources["owner"]
                    and database[3] == backend.url.username
                ):
                    raise RuntimeError("Image-import database identity changed")
                location = await db.scalar(
                    select(StorageLocation)
                    .where(StorageLocation.id == LEGACY_LOCATION_ID)
                    .with_for_update(read=True, nowait=True)
                )
                binding = await db.scalar(
                    select(StorageBinding)
                    .where(StorageBinding.purpose == "datasets")
                    .with_for_update(read=True, nowait=True)
                )
                gate = await db.scalar(
                    select(StorageMaintenanceGate)
                    .where(StorageMaintenanceGate.location_id == LEGACY_LOCATION_ID)
                    .with_for_update(read=True, nowait=True)
                )
                if not (
                    str(LEGACY_LOCATION_ID) == proof["location_id"]
                    and location is not None
                    and location.enabled
                    and location.kind == "s3"
                    and location.connection_id is None
                    and location.config_revision == 1
                    and location.config
                    == {
                        "endpoint": backend.settings.minio_endpoint,
                        "use_ssl": backend.settings.minio_use_ssl,
                    }
                    and binding is not None
                    and binding.location_id == LEGACY_LOCATION_ID
                    and binding.generation == 1
                    and binding.bucket == bucket
                    and gate is not None
                    and gate.state == "draining"
                    and gate.revision == proof["gate_revision"]
                ):
                    raise RuntimeError("Image-import admission or target changed")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND backend_type='client backend')"
                    )
                ):
                    raise RuntimeError("Image-import database sessions remain")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.storage_operations "
                        "WHERE location_id=:location)"
                    ),
                    {"location": LEGACY_LOCATION_ID},
                ):
                    raise RuntimeError("Legacy storage operations remain unsettled")
                if (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.managed_original_creations "
                            "WHERE location_id=:location"
                        ),
                        {"location": LEGACY_LOCATION_ID},
                    )
                    != proof["creations"]
                ):
                    raise RuntimeError("Legacy creation ledger changed")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.managed_original_creations "
                        "WHERE storage_identity=:identity AND location_id<>:location)"
                    ),
                    {
                        "identity": proof["storage_identity"],
                        "location": LEGACY_LOCATION_ID,
                    },
                ):
                    raise RuntimeError("Legacy bucket has a different registered owner")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.managed_original_creations "
                        "WHERE location_id=:location AND (("
                        "status='verified' AND bucket=:bucket AND storage_identity=:identity "
                        "AND settled_at IS NOT NULL AND object_key IS NOT NULL "
                        "AND object_key <> '' AND version_id IS NOT NULL "
                        "AND version_id NOT IN ('','null')) OR "
                        "(status='failed' AND error_code='storage_unavailable' "
                        "AND prepared IS NULL AND receipt IS NULL AND write_started_at IS NULL)) "
                        "IS NOT TRUE)"
                    ),
                    {
                        "location": LEGACY_LOCATION_ID,
                        "bucket": bucket,
                        "identity": proof["storage_identity"],
                    },
                ):
                    raise RuntimeError("Legacy creation outcome changed")
                verified_rows = (
                    (
                        await db.execute(
                            text(
                                "SELECT id,object_key,version_id FROM public.managed_original_creations "
                                "WHERE location_id=:location AND status='verified' ORDER BY id"
                            ),
                            {"location": LEGACY_LOCATION_ID},
                        )
                    )
                    .mappings()
                    .all()
                )
                verified_creations = [
                    {
                        "id": str(row["id"]),
                        "key": row["object_key"],
                        "version": row["version_id"],
                    }
                    for row in verified_rows
                ]
                if verified_creations != proof["verified_creations"]:
                    raise RuntimeError("Legacy verified creation identities changed")
                if await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.storage_usage_mutations "
                        "WHERE scope_key=:scope AND state IN ('pending','unknown'))"
                    ),
                    {"scope": proof["usage_scope"]},
                ):
                    raise RuntimeError("Legacy storage accounting became unsettled")
                if not require_owner(
                    bucket, backend.bucket_info(bucket), backend.resources["owner"]
                ):
                    raise RuntimeError("Image-import bucket identity changed")
                if (
                    backend.s3.get_bucket_versioning(Bucket=bucket).get("Status")
                    != "Enabled"
                ):
                    raise RuntimeError("Image-import bucket versioning changed")
                for page in backend.s3.get_paginator("list_multipart_uploads").paginate(
                    Bucket=bucket
                ):
                    if page.get("Uploads"):
                        raise RuntimeError("Image-import multipart uploads remain")
                versions = []
                for page in backend.s3.get_paginator("list_object_versions").paginate(
                    Bucket=bucket
                ):
                    for item in page.get("Versions", []) + page.get(
                        "DeleteMarkers", []
                    ):
                        key, version = item.get("Key"), item.get("VersionId")
                        if not isinstance(key, str) or version in (None, "", "null"):
                            raise RuntimeError(
                                "Image-import object identity is unproven"
                            )
                        versions.append({"key": key, "version": version})
                if len({(item["key"], item["version"]) for item in versions}) != len(
                    versions
                ):
                    raise RuntimeError("Image-import object inventory is inconsistent")
                retired = []
                if journal["phase"] == "retiring_versions":
                    recorded, retired = journal.get("versions"), journal.get("retired")
                    if (
                        not isinstance(recorded, list)
                        or not isinstance(retired, list)
                        or retired != recorded[: len(retired)]
                        or {(item["key"], item["version"]) for item in versions}
                        != {
                            (item["key"], item["version"])
                            for item in recorded[len(retired) :]
                        }
                    ):
                        raise RuntimeError("Image-import retirement inventory changed")
                inventory = {(item["key"], item["version"]) for item in versions}
                acknowledged = {(item["key"], item["version"]) for item in retired}
                accounted = inventory | acknowledged
                missing = [
                    item
                    for item in verified_creations
                    if (item["key"], item["version"]) not in accounted
                ]
                if missing:
                    journal["missing_verified_versions"] = missing
                    atomic_json(path, journal)
                    raise RuntimeError("Legacy verified creation version is missing")
                if journal["phase"] == "identity_disabled":
                    journal.update(
                        phase="retiring_versions",
                        versions=versions,
                        retired=[],
                        pending=None,
                    )
                    atomic_json(path, journal)
                else:
                    versions = recorded[len(retired) :]
                for item in versions:
                    journal["pending"] = item
                    atomic_json(path, journal)
                    try:
                        result = backend.s3.delete_object(
                            Bucket=bucket, Key=item["key"], VersionId=item["version"]
                        )
                    except ClientError as error:
                        response = error.response
                        if response.get("ResponseMetadata", {}).get(
                            "HTTPStatusCode"
                        ) == 403 and response.get("Error", {}).get("Code") in {
                            "403",
                            "AccessDenied",
                        }:
                            journal["pending"] = None
                            journal["last_denial"] = item
                            atomic_json(path, journal)
                        raise
                    if (
                        result.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        not in (200, 204)
                        or result.get("VersionId") != item["version"]
                    ):
                        raise RuntimeError(
                            "Image-import version retirement is unproven"
                        )
                    journal["retired"].append(item)
                    journal["pending"] = None
                    atomic_json(path, journal)
                for page in backend.s3.get_paginator("list_object_versions").paginate(
                    Bucket=bucket
                ):
                    if page.get("Versions") or page.get("DeleteMarkers"):
                        raise RuntimeError(
                            "Image-import bucket still contains versions"
                        )
                if not require_owner(
                    bucket, backend.bucket_info(bucket), backend.resources["owner"]
                ):
                    raise RuntimeError("Image-import bucket identity changed")
                journal["phase"] = "bucket_delete_intent"
                atomic_json(path, journal)
                response = backend.s3.delete_bucket(Bucket=bucket)
                if (
                    response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    not in (
                        200,
                        204,
                    )
                    or backend.bucket_info(bucket) is not None
                ):
                    raise RuntimeError("Image-import bucket retirement is unproven")
                journal["phase"] = "bucket_retired"
                atomic_json(path, journal)
    finally:
        await engine.dispose()


def _probe_image_import_partial_retirement(backend, prepared, scope, proof) -> dict:
    """Prove a real denied second delete keeps the live ledger and journal."""
    from botocore.exceptions import ClientError
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from worktree_env import atomic_json, read_state
    from worktree_resources import require_owner

    journal, path = scope["journal"], scope["journal_path"]
    bucket = backend.resources["buckets"]["MINIO_DATASETS_BUCKET"]
    key = "!aap-retirement-probe/" + uuid4().hex
    probe = _cleanup_storage_scope(backend)
    record = {
        "state": "identity_creating",
        "access_key": probe["access_key"],
        "secret_key": probe["secret_key"],
        "policy": probe["policy"],
        "config": probe["config"],
        "container": probe["container"],
        "key": key,
    }
    journal["partial_probe"] = record
    atomic_json(path, journal)
    _cleanup_mc(
        probe,
        ["admin", "user", "add", "scoped", probe["access_key"], probe["secret_key"]],
    )
    probe["user_created"] = True
    record["state"] = "identity_created"
    atomic_json(path, journal)
    bucket_arn = f"arn:aws:s3:::{bucket}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:ListBucketMultipartUploads",
                    "s3:GetBucketLocation",
                    "s3:GetBucketTagging",
                    "s3:GetBucketVersioning",
                ],
                "Resource": [bucket_arn],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:DeleteObjectVersion"],
                "Resource": [f"{bucket_arn}/{key}"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:DeleteObject"],
                "Resource": [f"{bucket_arn}/{key}"],
                "Condition": {
                    "StringLike": {"s3:versionid": "*?*"},
                    "StringNotEquals": {"s3:versionid": "null"},
                },
            },
        ],
    }
    _cleanup_mc(
        probe,
        ["admin", "policy", "create", "scoped", probe["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    probe["policy_created"] = True
    record["state"] = "policy_created"
    atomic_json(path, journal)
    _cleanup_mc(
        probe,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            probe["policy"],
            "--user",
            probe["access_key"],
        ],
    )
    record["state"] = "canary_put_intent"
    atomic_json(path, journal)
    response = backend.s3.put_object(Bucket=bucket, Key=key, Body=b"retirement probe")
    version = response.get("VersionId")
    if version in (None, "", "null"):
        raise RuntimeError("Retirement probe has no immutable version")
    record["version"] = version
    record["state"] = "canary_created"
    atomic_json(path, journal)

    def versions():
        return [
            {"key": item["Key"], "version": item["VersionId"]}
            for page in backend.s3.get_paginator("list_object_versions").paginate(
                Bucket=bucket
            )
            for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]

    before = versions()
    canary = {"key": key, "version": version}
    if len(before) < 2 or before[0] != canary:
        raise RuntimeError("Retirement probe inventory is not independently ordered")
    restricted = _restricted_s3_client(backend, probe)
    administrator = backend.s3
    try:
        backend.s3 = restricted
        try:
            asyncio.run(_retire_image_import_bucket(backend, prepared, scope, proof))
        except ClientError as error:
            if not (
                error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
                and error.response.get("Error", {}).get("Code")
                in {"403", "AccessDenied"}
            ):
                raise
        else:
            raise RuntimeError(
                "Retirement probe unexpectedly deleted a foreign version"
            )
    finally:
        backend.s3 = administrator
        restricted.close()
    if not (
        journal.get("phase") == "retiring_versions"
        and journal.get("pending") is None
        and journal.get("versions") == before
        and journal.get("retired") == [canary]
        and journal.get("last_denial") == before[1]
        and versions() == before[1:]
        and require_owner(
            bucket, backend.bucket_info(bucket), backend.resources["owner"]
        )
    ):
        raise RuntimeError("Partial retirement did not retain exact owned evidence")
    if read_state(path) != journal:
        raise RuntimeError("Partial retirement journal was not retained")
    for account, expected in ((scope, "disabled"), (probe, "enabled")):
        info = json.loads(
            _cleanup_mc(
                account, ["admin", "user", "info", "scoped", account["access_key"]]
            )
        )
        if info.get("userStatus") != expected:
            raise RuntimeError("Partial retirement identity changed")

    async def ledger_is_live():
        engine = create_async_engine(
            backend.url, poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.connect() as db:
                return (
                    await db.execute(
                        text(
                            "SELECT d.oid,g.state,g.revision FROM pg_catalog.pg_database d "
                            "JOIN public.storage_maintenance_gates g ON g.location_id=:location "
                            "WHERE d.datname=current_database()"
                        ),
                        {"location": LEGACY_LOCATION_ID},
                    )
                ).one()
        finally:
            await engine.dispose()

    ledger = asyncio.run(ledger_is_live())
    if (ledger[0], ledger[1], ledger[2]) != (
        proof["database_oid"],
        "draining",
        proof["gate_revision"],
    ):
        raise RuntimeError("Partial retirement lost its database or admission fence")
    record["state"] = "identity_removing"
    atomic_json(path, journal)
    _destroy_cleanup_storage_scope(probe)
    record.pop("secret_key")
    record["state"] = "retired"
    atomic_json(path, journal)
    return {
        "deleted_probe_version": True,
        "next_version_denied": True,
        "ledger_live": True,
    }


def _cleanup_mc(
    scope: dict, arguments: list[str], *, payload: str | None = None
) -> str:
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
    return result.stdout


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
                "Effect": "Deny",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": [
                    f"{cache_arn}/image-pyramids/objects/*",
                    f"{cache_arn}/_aap_managed/media-cache/image-pyramids/objects/*",
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


NATIVE_SOURCE_WRITER_TOKEN_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_WRITER_TOKEN"
NATIVE_SOURCE_READER_TOKEN_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_READER_TOKEN"
NATIVE_SOURCE_CLEANUP_TOKEN_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CLEANUP_TOKEN"
)
NATIVE_SOURCE_S3_ACCESS_KEY_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_S3_ACCESS_KEY"
)
NATIVE_SOURCE_S3_SECRET_KEY_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_S3_SECRET_KEY"
)
NATIVE_CONNECTION_S3_ACCESS_KEY_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CONNECTION_S3_ACCESS_KEY"
)
NATIVE_CONNECTION_S3_SECRET_KEY_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CONNECTION_S3_SECRET_KEY"
)


def _native_storage_scope(backend) -> dict:
    """Seed one scoped Native S3 identity against the established MinIO admin."""
    scope = _cleanup_storage_scope(backend)
    scope["policy"] = "aap-native-" + secrets.token_hex(8)
    scope["access_key"] = "aapnat" + secrets.token_hex(7)
    return scope


def _create_native_storage_scope(scope: dict, policy: dict) -> None:
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


def _native_delete_versions(backend, bucket: str, key: str) -> None:
    from botocore.exceptions import ClientError

    try:
        versions = backend.s3.list_object_versions(Bucket=bucket, Prefix=key)
    except ClientError:
        return
    for entry in versions.get("Versions", []) + versions.get("DeleteMarkers", []):
        if entry.get("Key") != key:
            continue
        backend.s3.delete_object(Bucket=bucket, Key=key, VersionId=entry["VersionId"])


def _verify_native_storage_scopes(
    backend, native: dict, connection: dict, cleanup: dict
) -> dict:
    """Prove the Source Native identity is finite and the lanes stay separated.

    Root admin only provisions; every assertion below issues actual S3
    requests with the scoped identities the runtimes will receive.
    """
    from botocore.exceptions import ClientError
    from native_storage_identity import NATIVE_CONNECTION_BASE_PREFIX

    cache_bucket = native["bucket"]
    connection_bucket = connection["bucket"]
    datasets_bucket = backend.resources["buckets"]["MINIO_DATASETS_BUCKET"]
    identity = uuid4()
    native_prefix = f"image-pyramids/objects/{identity}/"
    native_key = native_prefix + "lifecycle-probe"
    client = _restricted_s3_client(backend, native)
    connection_client = _restricted_s3_client(backend, connection)
    cleanup_client = _restricted_s3_client(backend, cleanup)
    created: list[tuple[str, str]] = []
    uploads: list[tuple[str, str, str]] = []
    try:
        put = client.put_object(
            Bucket=cache_bucket,
            Key=native_key,
            Body=b"native-probe",
            IfNoneMatch="*",
        )
        created.append((cache_bucket, native_key))
        recorded_version = put.get("VersionId")
        if recorded_version is not None and not isinstance(recorded_version, str):
            raise RuntimeError("Native probe returned an invalid version receipt")
        params = {
            "Bucket": cache_bucket,
            "Key": native_key,
            **({"VersionId": recorded_version} if recorded_version is not None else {}),
        }
        head = client.head_object(**params)
        if head.get("ETag") != put.get("ETag"):
            raise RuntimeError("Native probe HEAD did not prove its own receipt")
        stream = client.get_object(**params)["Body"]
        try:
            body = stream.read()
        finally:
            stream.close()
        if body != b"native-probe":
            raise RuntimeError("Native probe read did not prove its own bytes")
        listed = {
            item["Key"]
            for page in client.get_paginator("list_object_versions").paginate(
                Bucket=cache_bucket, Prefix=native_prefix
            )
            for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
        }
        if native_key not in listed:
            raise RuntimeError("Native probe is absent from its exact saved scope")
        _expect_s3_denied(
            "Native identity legacy pyramid write",
            lambda: client.put_object(
                Bucket=cache_bucket,
                Key=f"image-pyramids/{uuid4()}/legacy-probe",
                Body=b"forbidden",
            ),
        )
        _expect_s3_denied(
            "Native identity legacy thumbnail write",
            lambda: client.put_object(
                Bucket=cache_bucket,
                Key=f"image-assets/{uuid4()}/thumbnail-probe",
                Body=b"forbidden",
            ),
        )
        _expect_s3_denied(
            "Native identity foreign managed namespace write",
            lambda: client.put_object(
                Bucket=cache_bucket,
                Key=f"_aap_managed/media-cache/image-assets/{uuid4()}/probe",
                Body=b"forbidden",
            ),
        )
        _expect_s3_denied(
            "Native identity foreign bucket read",
            lambda: client.get_object(Bucket=datasets_bucket, Key="probe"),
        )
        source_key = f"image-source/{uuid4()}/probe"
        backend.s3.put_object(Bucket=datasets_bucket, Key=source_key, Body=b"source")
        created.append((datasets_bucket, source_key))
        _expect_s3_denied(
            "Native identity foreign bucket delete",
            lambda: client.delete_object(Bucket=datasets_bucket, Key=source_key),
        )
        upload = backend.s3.create_multipart_upload(
            Bucket=cache_bucket, Key=f"native-probe-upload/{uuid4()}"
        )
        uploads.append((upload["Bucket"], upload["Key"], upload["UploadId"]))
        pages = client.list_multipart_uploads(Bucket=cache_bucket, MaxUploads=100)
        observed = {item.get("UploadId") for item in pages.get("Uploads", []) or []}
        if upload["UploadId"] not in observed:
            raise RuntimeError("Native identity cannot prove bounded upload absence")
        _expect_s3_denied(
            "Native identity multipart abort",
            lambda: client.abort_multipart_upload(
                Bucket=upload["Bucket"],
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            ),
        )
        managed_key = f"_aap_managed/media-cache/image-pyramids/objects/{uuid4()}/owned"
        legacy_native_key = f"image-pyramids/objects/{uuid4()}/owned"
        for key in (managed_key, legacy_native_key):
            managed_receipt = client.put_object(
                Bucket=cache_bucket, Key=key, Body=b"owned", IfNoneMatch="*"
            )
            created.append((cache_bucket, key))
            managed_head = client.head_object(Bucket=cache_bucket, Key=key)
            if managed_head.get("ETag") != managed_receipt.get(
                "ETag"
            ) or managed_head.get("VersionId") != managed_receipt.get("VersionId"):
                raise RuntimeError("Native root receipt differs from the saved object")
            _expect_s3_denied(
                "Ordinary cleanup Native-root delete",
                lambda key=key: cleanup_client.delete_object(
                    Bucket=cache_bucket, Key=key
                ),
            )
            if managed_receipt.get("VersionId") is not None:
                _expect_s3_denied(
                    "Ordinary cleanup Native-root version delete",
                    lambda key=key, version=managed_receipt["VersionId"]: (
                        cleanup_client.delete_object(
                            Bucket=cache_bucket, Key=key, VersionId=version
                        )
                    ),
                )
        connection_key = (
            f"{NATIVE_CONNECTION_BASE_PREFIX}/_aap_managed/media-cache/"
            f"image-pyramids/objects/{uuid4()}/probe"
        )
        receipt = connection_client.put_object(
            Bucket=connection_bucket,
            Key=connection_key,
            Body=b"connection-probe",
            IfNoneMatch="*",
        )
        created.append((connection_bucket, connection_key))
        stream = connection_client.get_object(
            Bucket=connection_bucket, Key=connection_key
        )["Body"]
        try:
            if stream.read() != b"connection-probe":
                raise RuntimeError(
                    "Registered connection identity cannot read its scope"
                )
        finally:
            stream.close()
        exclude = {
            "Bucket": connection_bucket,
            "Key": connection_key,
            **(
                {"VersionId": receipt["VersionId"]}
                if receipt.get("VersionId") is not None
                else {}
            ),
        }
        connection_client.delete_object(**exclude)
        _expect_s3_denied(
            "Registered connection identity builtin Native read",
            lambda: connection_client.get_object(Bucket=cache_bucket, Key=native_key),
        )
        _expect_s3_denied(
            "Registered connection identity foreign key write",
            lambda: connection_client.put_object(
                Bucket=connection_bucket,
                Key=f"unmanaged/{uuid4()}/probe",
                Body=b"forbidden",
            ),
        )
        client.delete_object(**params)
        try:
            client.head_object(**params)
        except ClientError as exc:
            if exc.response["ResponseMetadata"][
                "HTTPStatusCode"
            ] != 404 or exc.response["Error"]["Code"] not in {
                "404",
                "NoSuchKey",
                "NoSuchVersion",
            }:
                raise
        else:
            raise RuntimeError("Native identity did not delete its exact saved object")
        return {
            "bucket": cache_bucket,
            "connection_bucket": connection_bucket,
            "native_prefix_listed": True,
            "native_put_get_head_delete": True,
            "native_managed_root_put_head": True,
            "legacy_and_foreign_denied": True,
            "multipart_listed_and_abort_denied": True,
            "cleanup_native_roots_denied": True,
            "connection_scope_bounded": True,
        }
    finally:
        client.close()
        connection_client.close()
        cleanup_client.close()
        for bucket, key, upload_id in uploads:
            try:
                backend.s3.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            except ClientError:
                pass
        for bucket, key in created:
            _native_delete_versions(backend, bucket, key)


def _create_api_bootstrap_storage_scope(
    scope: dict, *, e2e_fixtures: bool = False, workspace_guides: bool = False
) -> None:
    """Grant startup metadata access and only the requested API consumer's objects."""
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
    if workspace_guides:
        policy["Statement"].append(
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
                "Resource": [
                    f"arn:aws:s3:::{buckets['MINIO_BUCKET']}/projects/*/guide/*"
                ],
            }
        )
    if e2e_fixtures:
        policy["Statement"].append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [
                    f"arn:aws:s3:::{buckets['MINIO_DATASETS_BUCKET']}/{prefix}"
                    for prefix in (
                        "e2e/owned/*/image/*",
                        "e2e/owned/*/filtering/*",
                        "e2e/owned/*/project-roles/*",
                        "e2e/lidar/*",
                    )
                ]
                + [f"arn:aws:s3:::{buckets['MINIO_BUCKET']}/raster-masks/sha256/*"],
            }
        )
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


def _create_audit_archive_storage_scope(scope: dict, *object_actions: str) -> None:
    bucket = f"arn:aws:s3:::{scope['bucket']}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [bucket]},
            {
                "Effect": "Allow",
                "Action": list(object_actions),
                "Resource": [f"{bucket}/*"],
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


def _create_image_import_storage_scope(scope: dict) -> None:
    """Give this exclusive profile only managed-original creation and readback."""
    from worktree_env import atomic_json

    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    scope["journal"]["phase"] = "identity_created"
    atomic_json(scope["journal_path"], scope["journal"])
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/__aap_managed_datasets__/*"
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
    scope["journal"]["phase"] = "policy_created"
    atomic_json(scope["journal_path"], scope["journal"])
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
    scope["journal"]["phase"] = "identity_ready"
    atomic_json(scope["journal_path"], scope["journal"])


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
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
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
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
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


def _fixture_provider_host(container_id: str) -> str:
    """The verified bridge gateway that the owned PostgreSQL container reaches.

    The fixture backend binds 0.0.0.0 but refuses loopback addresses because
    workers are separate processes. This owned container's network gateway is
    the host address reachable both from the host and from that bridge.
    """
    from worktree_resources import docker

    output = docker(
        "inspect",
        "--format",
        "{{range .NetworkSettings.Networks}}{{.Gateway}}\n{{end}}",
        container_id,
    )
    candidates = set()
    for line in output.splitlines():
        gateway = line.strip()
        if not gateway:
            continue
        address = ipaddress.ip_address(gateway)
        if address.version == 4 and not (address.is_loopback or address.is_unspecified):
            candidates.add(gateway)
    if len(candidates) != 1:
        raise RuntimeError("owned fixture provider bridge gateway is ambiguous")
    return candidates.pop()


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
        guards = re.findall(r"RAISE EXCEPTION '([^']+)';", sql)
        diagnostic = next(
            (message for message in guards if f"ERROR:  {message}" in result.stderr),
            "unclassified SQL failure",
        )
        raise RuntimeError(
            f"psql acceptance phase failed with exit {result.returncode}: {diagnostic}"
        )
    return result.stdout


def _ordinary_login_acl(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    username: str,
    password: str,
    database: str,
    source_signatures: tuple[str, ...],
) -> dict:
    private_signatures = ", ".join(
        f"'{signature}'"
        for signature in sorted({*PRIVATE_RUNTIME_FUNCTIONS, *source_signatures})
    )
    ordinary_signatures = ", ".join(
        f"'{signature}'"
        for signature in (
            *IDENTITY_AVATAR_FUNCTIONS,
            *IDENTITY_AVATAR_INVENTORY_FUNCTIONS,
            "yoao_jobs_v1.directory_job_total(uuid,uuid)",
            "yoao_assets_v1.read_legacy_pyramid_summaries(uuid[],uuid[],text)",
            "yoao_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid,uuid,text,text)",
            "yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision)",
            "yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)",
        )
    )
    return json.loads(
        _psql(
            container_id,
            container_name,
            container_identity,
            username=username,
            password=password,
            database=database,
            sql=f"""
WITH RECURSIVE scoped(oid) AS (
    SELECT c.oid
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE (n.nspname = 'public' OR n.nspname ~ '^(aap_|yoao_)')
      AND c.relkind IN ('r', 'p', 'm', 'f', 'S')
    UNION
    SELECT i.inhrelid FROM pg_catalog.pg_inherits i
    JOIN scoped ON scoped.oid = i.inhparent
), relations AS (
    SELECT c.oid, c.relkind, pg_catalog.format('%I.%I', n.nspname, c.relname) AS name
    FROM scoped
    JOIN pg_catalog.pg_class c ON c.oid = scoped.oid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
), routines AS (
    SELECT p.oid, n.oid AS schema_oid, n.nspname || '.' || p.proname || '(' ||
        pg_catalog.replace(pg_catalog.oidvectortypes(p.proargtypes), ', ', ',') || ')' AS signature
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname ~ '^(aap_|yoao_)'
)
SELECT pg_catalog.jsonb_build_object(
    'login', session_user,
    'effective_role', current_user,
    'database_oid', (SELECT oid::bigint FROM pg_catalog.pg_database
                     WHERE datname = current_database()),
    'restricted_login', (SELECT rolcanlogin AND NOT (rolsuper OR rolcreatedb
        OR rolcreaterole OR rolreplication OR rolbypassrls)
        AND NOT EXISTS (SELECT FROM pg_catalog.pg_auth_members WHERE member = r.oid)
        FROM pg_catalog.pg_roles r WHERE r.rolname = session_user),
    'scanned_private_relations', (SELECT count(*) FROM relations WHERE relkind <> 'S'),
    'scanned_private_sequences', (SELECT count(*) FROM relations WHERE relkind = 'S'),
    'unexpected_private_relations', ARRAY(
        SELECT name FROM relations
        WHERE CASE WHEN relkind <> 'S' THEN
            pg_catalog.has_table_privilege(oid,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                || CASE WHEN current_setting('server_version_num')::integer >= 170000
                        THEN ',MAINTAIN' ELSE '' END)
            OR pg_catalog.has_any_column_privilege(oid, 'SELECT,INSERT,UPDATE,REFERENCES')
            ELSE false END
        ORDER BY name),
    'unexpected_private_sequences', ARRAY(
        SELECT name FROM relations
        WHERE CASE WHEN relkind = 'S' THEN
            pg_catalog.has_sequence_privilege(oid, 'SELECT,UPDATE,USAGE')
            ELSE false END
        ORDER BY name),
    'unexpected_private_functions', ARRAY(
        SELECT expected.signature FROM unnest(ARRAY[{private_signatures}]) AS expected(signature)
        LEFT JOIN routines USING (signature)
        WHERE routines.oid IS NULL OR pg_catalog.has_function_privilege(routines.oid, 'EXECUTE')
        ORDER BY signature),
    'missing_business_functions', ARRAY(
        SELECT expected.signature FROM unnest(ARRAY[{ordinary_signatures}]) AS expected(signature)
        LEFT JOIN routines USING (signature)
        WHERE routines.oid IS NULL OR NOT pg_catalog.has_function_privilege(routines.oid, 'EXECUTE')
           OR NOT pg_catalog.has_schema_privilege(routines.schema_oid, 'USAGE')
        ORDER BY signature)
)::text;
""",
        )
    )


def _verify_inference_project_grant_reapplication(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
    grant_sql: str,
    variables: dict[str, str],
    evidence: dict,
) -> None:
    """Exercise removal of pre-existing Project column access in the owned DB."""

    def run(sql: str, *, script_variables: dict[str, str] | None = None) -> str:
        return _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=sql,
            variables=script_variables,
        )

    def privileges() -> dict:
        return json.loads(
            run(
                "SELECT pg_catalog.json_build_object("
                "'project_id',pg_catalog.has_column_privilege("
                "'inference_code','public.projects','id','SELECT'),"
                "'project_pool',pg_catalog.has_column_privilege("
                "'inference_code','public.projects','ml_backend_pool_id','SELECT'),"
                "'project_table',pg_catalog.has_table_privilege("
                "'inference_code','public.projects','SELECT'),"
                "'workspace_project_id',pg_catalog.has_column_privilege("
                "'workspace_code','public.projects','id','SELECT'),"
                "'registry_select',pg_catalog.has_table_privilege("
                "'inference_code','public.ml_backend_registry','SELECT'),"
                "'project_lock',pg_catalog.has_function_privilege("
                "'inference_code','yoao_workspace_v1.lock_project_reference(uuid)','EXECUTE'),"
                "'binding_clear',pg_catalog.has_function_privilege("
                "'inference_code','yoao_workspace_v1.clear_primary_backend_bindings(uuid)','EXECUTE'));"
            )
        )

    expected = {
        "project_id": False,
        "project_pool": False,
        "project_table": False,
        "workspace_project_id": True,
        "registry_select": True,
        "project_lock": True,
        "binding_clear": True,
    }
    initial = privileges()
    evidence["fresh"] = initial
    if initial != expected:
        raise RuntimeError(
            "Fresh Inference Project privileges differ from the fixed boundary"
        )
    run("GRANT SELECT (id, ml_backend_pool_id) ON public.projects TO inference_code;")
    staged = privileges()
    evidence["preexisting"] = staged
    if staged != {**expected, "project_id": True, "project_pool": True}:
        raise RuntimeError(
            "Could not reproduce the prior Inference Project column grants"
        )
    run(grant_sql, script_variables=variables)
    restored = privileges()
    evidence["reapplied"] = restored
    if restored != expected:
        raise RuntimeError(
            "Reapplied module grants did not remove old Inference Project access"
        )


def _assert_psql_permission_denied(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    username: str,
    password: str,
    database: str,
    statement: str,
) -> None:
    escaped = statement.replace("'", "''")
    _psql(
        container_id,
        container_name,
        container_identity,
        username=username,
        password=password,
        database=database,
        sql=(
            "DO $$ DECLARE denied boolean := false; BEGIN "
            "BEGIN EXECUTE '"
            f"{escaped}"
            "'; EXCEPTION WHEN insufficient_privilege THEN denied := true; "
            "END; IF NOT denied THEN RAISE EXCEPTION "
            "'expected SQLSTATE 42501 for a restricted read role'; END IF; END $$;"
        ),
    )


def _sql_artifact_path(name: str) -> Path:
    if name in JOBS_SQL_ARTIFACTS:
        directory = ROOT / "apps/jobs/src/yoao_jobs/sql"
    elif name in ASSETS_SOURCE_SQL_ARTIFACTS:
        directory = ROOT / "apps/assets-source/src/yoao_assets_source"
    elif name == "0484_notifications_terminal_outbox.sql":
        directory = ROOT / "apps/notifications/src/yoao_notifications/sql"
    elif name == "0485_audit_platform_maintenance.sql":
        directory = ROOT / "apps/audit/src/yoao_audit/sql"
    elif name in PACKAGED_CURRENT_SQL:
        directory = ROOT / PACKAGED_CURRENT_SQL[name]
    else:
        directory = ROOT / "apps/api/alembic/sql"
    return directory / name


def _installed_function_body_sha256(
    signature: str, sql_name: str, actual: str | None
) -> str:
    if actual is None:
        raise RuntimeError(f"installed function body is missing: {signature}")
    base_sql_name = {
        "0405_video_tracker_asset_candidates.sql": "0281_video_execution.sql",
        "0450_video_candidate_status_restore.sql": "0284_video_restore.sql",
        "0475_inbox_retained_job_scope.sql": "0421_notification_job_scope.sql",
    }.get(sql_name, sql_name)
    if (
        signature
        == "yoao_assets_v1.complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)"
    ):
        base_sql_name = "0214_dataset_import_core.sql"
    source = _sql_artifact_path(base_sql_name).read_text(encoding="utf-8")
    current_name = signature.split("(", 1)[0]
    if "." in current_name:
        schema, function = current_name.split(".", 1)
        if base_sql_name in CURRENT_NAMESPACE_SQL:
            frozen_schema = schema
        else:
            frozen_schema = FROZEN_OWNER_NAMESPACES.get(schema, schema)
        function_name = re.escape(f"{frozen_schema}.{function}")
    else:
        schema = ""
        frozen_schema = ""
        function_name = re.escape(current_name)
    match = re.search(
        rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+{function_name}\s*\("
        r".*?\bAS\s+(?P<tag>\$(?:[a-z_]\w*)?\$)(?P<body>.*?)(?P=tag);",
        source,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError(f"frozen SQL has no function body: {sql_name}")
    expected = match.group("body")
    if base_sql_name != sql_name:
        patch_source = _sql_artifact_path(sql_name).read_text(encoding="utf-8")
        tags = ("before", "after")
        if sql_name == "0450_video_candidate_status_restore.sql":
            tags += ("guard", "candidates")
        if sql_name == "0453_import_original_verification.sql":
            tags += (
                "fields_before",
                "fields_after",
                "validation_before",
                "validation_after",
            )
        literals = {}
        for tag in tags:
            matches = re.findall(rf"\${tag}\$(.*?)\${tag}\$", patch_source, re.DOTALL)
            if len(matches) != 1:
                raise RuntimeError(
                    f"frozen SQL patch literal is not unique: {sql_name}"
                )
            literals[tag] = matches[0]
        replacements = [(literals["before"], literals["after"])]
        if "fields_before" in literals:
            replacements.insert(
                0, (literals["fields_before"], literals["fields_after"])
            )
            replacements.insert(
                1, (literals["validation_before"], literals["validation_after"])
            )
        if "guard" in literals:
            replacements.append(
                (literals["guard"], literals["guard"] + literals["candidates"])
            )
        for before, after in replacements:
            if expected.count(before) != 1 or after in expected:
                raise RuntimeError(f"frozen SQL patch source differs: {signature}")
            expected = expected.replace(before, after, 1)
    if signature == "yoao_assets_v1._image_owner_source_share(uuid,uuid,uuid,boolean)":
        task_item = "WHERE item.id = (v_task_data ->> 'dataset_item_id')::uuid\n                FOR "
        before, after = task_item + "UPDATE NOWAIT;", task_item + "SHARE NOWAIT;"
        if expected.count(before) != 1 or after in expected:
            raise RuntimeError("frozen image Task Item admission fence differs")
        expected = expected.replace(before, after, 1)
    if signature == "yoao_labeling_v1.mask_edit_command(jsonb)":
        marker = "    v_operation := p_request ->> 'operation';\n"
        if expected.count(marker) != 1:
            raise RuntimeError("frozen mask edit operation marker is not unique")
        expected = expected.replace(
            marker,
            marker
            + "    IF v_operation = 'acceptance_history' THEN\n"
            + "        RETURN yoao_labeling_v1._acceptance_history(p_request);\n"
            + "    END IF;\n",
            1,
        )
    if schema in FROZEN_OWNER_NAMESPACES or current_name in NAMESPACE_BODY_CALLERS:
        for current, frozen in FROZEN_OWNER_NAMESPACES.items():
            expected = expected.replace(f"{frozen}.", f"{current}.")
    for old, new in PROJECTED_GUCS.items():
        expected = expected.replace(old, new)
    if signature in KEY_USAGE_BODY_PATCHES:
        patch_sql = _sql_artifact_path("0479_identity_api_key_usage.sql").read_text(
            encoding="utf-8"
        )
        for tag, occurrences in KEY_USAGE_BODY_PATCHES[signature]:
            before, after = (
                re.findall(
                    rf"\${tag}_{side}\$(.*?)\${tag}_{side}\$", patch_sql, re.DOTALL
                )
                for side in ("before", "after")
            )
            if (
                len(before) != 1
                or len(after) != 1
                or expected.count(before[0]) != occurrences
            ):
                raise RuntimeError(
                    f"frozen key usage patch differs: {signature}, {tag}"
                )
            expected = expected.replace(before[0], after[0])
    if current_name in {
        "yoao_inference_gpu_caller_v1.gpu_caller_policy",
        "yoao_inference_gpu_collector_v1.gpu_collector_policy",
    }:
        expected = expected.replace(f"'{frozen_schema}'", f"'{schema}'")
    if actual != expected:
        raise RuntimeError(
            f"installed function body differs from frozen SQL: {signature}"
        )
    return hashlib.sha256(actual.encode("utf-8")).hexdigest()


def _installed_function_bodies(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
) -> dict[str, str]:
    bodies = json.loads(
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "SELECT pg_catalog.json_object_agg(s.signature, p.prosrc) "
                "FROM pg_catalog.json_array_elements_text(:'signatures'::json) AS s(signature) "
                "LEFT JOIN pg_catalog.pg_proc AS p ON p.oid = pg_catalog.to_regprocedure(s.signature)"
            ),
            variables={"signatures": json.dumps(list(FUNCTION_BODY_ARTIFACTS))},
        )
    )
    return {
        signature: _installed_function_body_sha256(
            signature, sql_name, bodies[signature]
        )
        for signature, sql_name in FUNCTION_BODY_ARTIFACTS.items()
    }


def _verify_audit_daily_projection_boundary(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
    runtime_password: str,
    read_password: str,
    refresh_password: str,
    refresh_url: str,
) -> dict:
    def execute(username: str, password: str, sql: str) -> str:
        return _psql(
            container_id,
            container_name,
            container_identity,
            username=username,
            password=password,
            database=database,
            sql=sql,
        )

    execute(
        "owner",
        owner_password,
        f'GRANT USAGE ON SCHEMA yoao_audit_v1 TO "{AUDIT_DAILY_REFRESH_ROLE}"; '
        f'GRANT EXECUTE ON FUNCTION yoao_audit_v1.refresh_audit_bi_mv() TO "{AUDIT_DAILY_REFRESH_ROLE}";',
    )

    async def verify_refresh_login() -> None:
        import asyncpg

        from yoao_owner_data.postgres import verify_function_only_role

        connection = await asyncpg.connect(refresh_url, timeout=5)
        try:
            await verify_function_only_role(
                connection,
                schema="yoao_audit_v1",
                functions={("refresh_audit_bi_mv", "")},
                role=AUDIT_DAILY_REFRESH_ROLE,
            )
        finally:
            await connection.close()

    asyncio.run(verify_refresh_login())
    ownership = json.loads(
        execute(
            "owner",
            owner_password,
            "SELECT json_build_object("
            "'mv_owner',pg_get_userbyid(mv.relowner),"
            "'view_owner',pg_get_userbyid(facts.relowner),"
            "'refresh_owner',pg_get_userbyid(refresh.proowner),"
            "'definer',refresh.prosecdef,"
            "'owner_temp',has_database_privilege('audit_code',current_database(),'TEMP'),"
            "'owner_public_create',has_schema_privilege('audit_code','public','CREATE'),"
            "'owner_audit_create',has_schema_privilege('audit_code','yoao_audit_v1','CREATE'),"
            "'refresh_temp',has_database_privilege("
            f"'{AUDIT_DAILY_REFRESH_ROLE}',current_database(),'TEMP')) "
            "FROM pg_class AS mv,pg_class AS facts,pg_proc AS refresh "
            "WHERE mv.oid='public.mv_audit_bi_daily'::regclass "
            "AND facts.oid='yoao_audit_v1.audit_daily_facts'::regclass "
            "AND refresh.oid='yoao_audit_v1.refresh_audit_bi_mv()'::regprocedure;",
        )
    )
    if ownership != {
        "mv_owner": "audit_code",
        "view_owner": "audit_code",
        "refresh_owner": "audit_code",
        "definer": True,
        "owner_temp": True,
        "owner_public_create": True,
        "owner_audit_create": False,
        "refresh_temp": False,
    }:
        raise RuntimeError("Audit daily ownership or fixed refresh privileges differ")

    action = f"probe.audit.daily.{uuid4().hex}"
    execute(
        "owner",
        owner_password,
        "INSERT INTO public.audit_logs(action,target_type,actor_role,status_code,created_at) "
        f"VALUES ('{action}','verifier','super_admin',200,"
        "(date_trunc('day',CURRENT_TIMESTAMP AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
        "-interval '1 hour');",
    )
    execute(
        AUDIT_DAILY_REFRESH_ROLE,
        refresh_password,
        "BEGIN; SELECT yoao_audit_v1.refresh_audit_bi_mv(); COMMIT;",
    )
    projected_count = execute(
        AUDIT_READ_ROLE,
        read_password,
        "SELECT event_count::text FROM yoao_audit_v1.audit_daily_facts "
        f"WHERE action='{action}' AND status_family=2;",
    ).strip()
    if projected_count != "1":
        raise RuntimeError(
            "The function-only refresh did not publish the tagged daily row"
        )

    for username, password, statements in (
        (
            "legacy_runtime",
            runtime_password,
            (
                "SELECT * FROM public.mv_audit_bi_daily LIMIT 1",
                "SELECT yoao_audit_v1.refresh_audit_bi_mv()",
            ),
        ),
        (
            AUDIT_READ_ROLE,
            read_password,
            (
                "SELECT * FROM public.mv_audit_bi_daily LIMIT 1",
                "SELECT yoao_audit_v1.refresh_audit_bi_mv()",
            ),
        ),
        (
            AUDIT_DAILY_REFRESH_ROLE,
            refresh_password,
            (
                "SELECT * FROM public.audit_logs LIMIT 1",
                "SELECT * FROM public.mv_audit_bi_daily LIMIT 1",
                "SELECT * FROM yoao_audit_v1.audit_daily_facts LIMIT 1",
            ),
        ),
    ):
        for statement in statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=username,
                password=password,
                database=database,
                statement=statement,
            )
    return {
        "ownership": ownership,
        "function_only_login": True,
        "committed_refresh_and_projection": True,
        "private_mv_and_unauthorized_refresh_denied": True,
        "probe_action": action,
        "probe_retention": "Append-only source row remains until exact owned DB destruction",
    }


def _verify_audit_maintenance_boundary(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
    runtime_password: str,
    maintenance_password: str,
    maintenance_url: str,
) -> dict:
    current_month = datetime.now(timezone.utc).date().replace(day=1)
    current_partition = f"audit_logs_y{current_month:%Y}m{current_month:%m}"
    for month in (current_month.isoformat(), "2098-04-01"):
        request = json.dumps(
            {
                "schema": "aap-audit-maintenance/v1",
                "operation": "ensure",
                "month": month,
            },
            separators=(",", ":"),
        )
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql="SELECT yoao_audit_v1.maintenance_command('"
            + request.replace("'", "''")
            + "'::jsonb);",
        )

    async def verify_maintenance_login() -> None:
        import asyncpg

        from yoao_owner_data.postgres import verify_function_only_role

        connection = await asyncpg.connect(maintenance_url, timeout=5)
        try:
            await verify_function_only_role(
                connection,
                schema="yoao_audit_v1",
                functions={("maintenance_command", "jsonb")},
                role=AUDIT_MAINTENANCE_ROLE,
            )
        finally:
            await connection.close()

    asyncio.run(verify_maintenance_login())
    ownership = json.loads(
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "SELECT json_build_object("
                "'role_profile', (SELECT rolcanlogin AND NOT rolinherit "
                "AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                "OR rolbypassrls) FROM pg_catalog.pg_roles "
                f"WHERE rolname='{AUDIT_MAINTENANCE_ROLE}'),"
                "'owner_parent', pg_catalog.pg_get_userbyid(parent.relowner),"
                "'owner_current_partition', pg_catalog.pg_get_userbyid(current_partition.relowner),"
                "'owner_future_partition', pg_catalog.pg_get_userbyid(future_partition.relowner),"
                "'owner_sequence', pg_catalog.pg_get_userbyid(sequence.relowner),"
                "'owner_temp', pg_catalog.has_database_privilege('audit_code',current_database(),'TEMP'),"
                "'owner_public_create', pg_catalog.has_schema_privilege('audit_code','public','CREATE'),"
                "'maintenance_connect', pg_catalog.has_database_privilege("
                f"'{AUDIT_MAINTENANCE_ROLE}',current_database(),'CONNECT'),"
                "'maintenance_no_create_or_temp', NOT pg_catalog.has_database_privilege("
                f"'{AUDIT_MAINTENANCE_ROLE}',current_database(),'CREATE,TEMP'),"
                "'maintenance_schema_usage_only', pg_catalog.has_schema_privilege("
                f"'{AUDIT_MAINTENANCE_ROLE}','yoao_audit_v1','USAGE') AND NOT "
                "pg_catalog.has_schema_privilege("
                f"'{AUDIT_MAINTENANCE_ROLE}','yoao_audit_v1','CREATE'),"
                "'maintenance_owns_no_objects', NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class "
                f"WHERE relowner='{AUDIT_MAINTENANCE_ROLE}'::pg_catalog.regrole) "
                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace "
                f"WHERE nspowner='{AUDIT_MAINTENANCE_ROLE}'::pg_catalog.regrole),"
                "'default_audit_storage_private', NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_default_acl AS defaults "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl "
                "WHERE defaults.defaclrole='audit_code'::pg_catalog.regrole "
                "AND defaults.defaclnamespace IN (0,'public'::pg_catalog.regnamespace) "
                "AND defaults.defaclobjtype IN ('r','S') "
                "AND acl.grantee<>'audit_code'::pg_catalog.regrole),"
                "'runtime_audit_tree_private', NOT EXISTS ("
                "SELECT FROM pg_catalog.pg_partition_tree(parent.oid::pg_catalog.regclass) AS partition "
                "WHERE pg_catalog.has_table_privilege('legacy_runtime',partition.relid,"
                "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                "OR pg_catalog.has_any_column_privilege('legacy_runtime',partition.relid,"
                "'SELECT,INSERT,UPDATE,REFERENCES'))) "
                "FROM pg_catalog.pg_class AS parent, pg_catalog.pg_class AS current_partition, "
                "pg_catalog.pg_class AS future_partition, pg_catalog.pg_class AS sequence "
                "WHERE parent.oid='public.audit_logs'::pg_catalog.regclass "
                f"AND current_partition.oid='public.{current_partition}'::pg_catalog.regclass "
                "AND future_partition.oid='public.audit_logs_y2098m04'::pg_catalog.regclass "
                "AND sequence.oid=pg_catalog.pg_get_serial_sequence("
                "'public.audit_logs','id')::pg_catalog.regclass;"
            ),
        )
    )
    expected_ownership = {
        "role_profile": True,
        "owner_parent": "audit_code",
        "owner_current_partition": "audit_code",
        "owner_future_partition": "audit_code",
        "owner_sequence": "audit_code",
        "owner_temp": True,
        "owner_public_create": True,
        "maintenance_connect": True,
        "maintenance_no_create_or_temp": True,
        "maintenance_schema_usage_only": True,
        "maintenance_owns_no_objects": True,
        "default_audit_storage_private": True,
        "runtime_audit_tree_private": True,
    }
    if ownership != expected_ownership:
        raise RuntimeError("Audit maintenance role, owner, or future ACL differs")

    direct_statements = (
        "SELECT id FROM {relation} LIMIT 0",
        "INSERT INTO {relation} (id, action, created_at) "
        "SELECT 9223372036854775806, 'probe.audit.denied', "
        f"TIMESTAMPTZ '{current_month.isoformat()} 00:00:00+00' WHERE false",
        "UPDATE {relation} SET action=action WHERE false",
        "DELETE FROM {relation} WHERE false",
    )
    for username, password in (
        ("legacy_runtime", runtime_password),
        (AUDIT_MAINTENANCE_ROLE, maintenance_password),
    ):
        for relation in (
            "public.audit_logs",
            f"public.{current_partition}",
            "public.audit_logs_y2098m04",
        ):
            for statement in direct_statements:
                _assert_psql_permission_denied(
                    container_id,
                    container_name,
                    container_identity,
                    username=username,
                    password=password,
                    database=database,
                    statement=statement.format(relation=relation),
                )
        ddl_statements = (
            "SELECT pg_catalog.nextval(pg_catalog.pg_get_serial_sequence("
            "'public.audit_logs','id')::pg_catalog.regclass)",
            "CREATE TABLE public.audit_maintenance_denied_probe(id integer)",
            "CREATE TABLE yoao_audit_v1.audit_maintenance_denied_probe(id integer)",
            "ALTER TABLE public.audit_logs ADD COLUMN audit_maintenance_denied_probe integer",
            "SELECT day FROM public.mv_audit_bi_daily LIMIT 0",
            "SELECT id FROM public.audit_archive_outputs LIMIT 0",
        )
        if username == AUDIT_MAINTENANCE_ROLE:
            ddl_statements += (
                "CREATE TEMP TABLE audit_maintenance_denied_probe(id integer)",
            )
        for statement in ddl_statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=username,
                password=password,
                database=database,
                statement=statement,
            )

    return {
        "function_only_login": True,
        "direct_partition_writes_denied": True,
        "direct_partition_reads_denied": True,
        "sequence_and_ddl_denied": True,
        "owner_and_future_acl_verified": True,
        "probe_partitions": [current_partition, "audit_logs_y2098m04"],
        "ownership": ownership,
    }


async def _verify_inbox_grant_guards(owner_url: str, profile_sql: str) -> dict:
    import asyncpg

    blocks = {}
    for name in ("guard", "verify"):
        match = re.search(rf"DO \${name}\$.*?\${name}\$;", profile_sql, re.DOTALL)
        if match is None:
            raise RuntimeError("Inbox grant profile is missing an acceptance guard")
        blocks[name] = match.group()
    connection = await asyncpg.connect(owner_url, timeout=5)
    probe_role = "inbox_owner_probe_" + uuid4().hex
    try:
        database = await connection.fetchval("SELECT current_database()")
        signatures = [*NOTIFICATION_INBOX_FUNCTIONS, NOTIFICATION_INBOX_PRIVATE_HELPER]
        oids = [
            await connection.fetchval(
                "SELECT pg_catalog.to_regprocedure($1)::oid", value
            )
            for value in signatures
        ]
        if any(value is None for value in oids):
            raise RuntimeError(
                "Inbox grant probe requires all four installed functions"
            )
        for label, mutation, block, expected_error in (
            (
                "owner_membership_rejected",
                f'CREATE ROLE "{probe_role}" NOLOGIN; '
                f'GRANT notifications_code TO "{probe_role}";',
                blocks["guard"],
                "Unsafe inbox owner, API or query LOGIN",
            ),
            (
                "public_functions_execute_rejected",
                "GRANT EXECUTE ON FUNCTION "
                + ",".join(NOTIFICATION_INBOX_FUNCTIONS)
                + " TO PUBLIC;",
                blocks["verify"],
                "PUBLIC retains inbox EXECUTE",
            ),
            (
                "public_helper_execute_rejected",
                f"GRANT EXECUTE ON FUNCTION {NOTIFICATION_INBOX_PRIVATE_HELPER} TO PUBLIC;",
                blocks["verify"],
                "PUBLIC retains inbox EXECUTE",
            ),
        ):
            transaction = connection.transaction()
            await transaction.start()
            try:
                for key, value in {
                    "expected_database": database,
                    "migration_role": "owner",
                    "notifications_owner": "notifications_code",
                    "api_role": "legacy_runtime",
                    "query_client": NOTIFICATION_INBOX_ROLE,
                }.items():
                    await connection.execute(
                        "SELECT pg_catalog.set_config($1,$2,true)", f"yoao.{key}", value
                    )
                await connection.execute(mutation)
                try:
                    async with connection.transaction():
                        await connection.execute(block)
                except asyncpg.RaiseError as error:
                    if str(error) != expected_error:
                        raise RuntimeError(
                            "Inbox ACL guard rejected an unrelated condition"
                        ) from None
                else:
                    raise RuntimeError(f"Inbox ACL guard failed: {label}")
            finally:
                await transaction.rollback()
        if await connection.fetchval(
            "SELECT EXISTS(SELECT FROM pg_catalog.pg_roles WHERE rolname=$1)",
            probe_role,
        ):
            raise RuntimeError("Inbox owner membership probe did not roll back")
        if await connection.fetchval(
            "SELECT EXISTS(SELECT FROM pg_catalog.pg_proc AS function "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,"
            "pg_catalog.acldefault('f',function.proowner))) AS acl "
            "WHERE function.oid=ANY($1::oid[]) AND acl.grantee=0 "
            "AND acl.privilege_type='EXECUTE')",
            oids,
        ):
            raise RuntimeError("PUBLIC retained inbox EXECUTE after probe rollback")
    finally:
        await connection.close()
    return {
        "owner_membership_rejected": True,
        "public_functions_execute_rejected": True,
        "public_helper_execute_rejected": True,
        "probe_role_and_acl_rolled_back": True,
    }


def _frozen_relation_name(name: str) -> str:
    schema, _, relation = name.partition(".")
    for current, frozen in FROZEN_OWNER_NAMESPACES.items():
        if schema == current:
            return f"{frozen}.{relation}"
    return name


def _project_relation_name(name: str) -> str:
    schema, _, relation = name.partition(".")
    for current, frozen in FROZEN_OWNER_NAMESPACES.items():
        if schema == frozen:
            return f"{current}.{relation}"
    return name


def _read_relation_evidence_expression(*, frozen: bool = False) -> str:
    targets = ", ".join(
        f"('{_frozen_relation_name(relation_name) if frozen else relation_name}')"
        for relation_name in READ_RELATION_SPECS
    )
    return f"""COALESCE((
        SELECT pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'relation', target.relation,
                'deparser_hex', pg_catalog.encode(
                    pg_catalog.convert_to(
                        pg_catalog.pg_get_viewdef(relation.oid, true), 'UTF8'
                    ),
                    'hex'
                ),
                'relkind', relation.relkind::text,
                'owner', pg_catalog.pg_get_userbyid(relation.relowner),
                'reloptions', COALESCE(
                    pg_catalog.to_jsonb(relation.reloptions), '[]'::pg_catalog.jsonb
                ),
                'columns', COALESCE((
                    SELECT pg_catalog.jsonb_agg(
                        pg_catalog.jsonb_build_object(
                            'name', attribute.attname,
                            'type', pg_catalog.format_type(
                                attribute.atttypid, attribute.atttypmod
                            )
                        ) ORDER BY attribute.attnum
                    )
                    FROM pg_catalog.pg_attribute AS attribute
                    WHERE attribute.attrelid = relation.oid
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                ), '[]'::pg_catalog.jsonb)
            ) ORDER BY target.relation
        )
        FROM (VALUES {targets}) AS target(relation)
        LEFT JOIN pg_catalog.pg_class AS relation
          ON relation.oid = pg_catalog.to_regclass(target.relation)::oid
    ), '[]'::pg_catalog.jsonb)"""


def _normalize_read_relation_observations(
    value: str,
    *,
    expected_owner: str | dict[str, str] | None = None,
    projected: bool = False,
) -> dict[str, dict]:
    try:
        observations = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("read relation catalog evidence is malformed") from None
    if not isinstance(observations, list):
        raise RuntimeError("read relation catalog evidence is not an array")

    normalized: dict[str, dict] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            raise RuntimeError("read relation catalog entry is malformed")
        relation_name = observation.get("relation")
        if projected and isinstance(relation_name, str):
            relation_name = _project_relation_name(relation_name)
        if relation_name not in READ_RELATION_SPECS or relation_name in normalized:
            raise RuntimeError("read relation catalog returned an unknown relation")
        definition_hex = observation.get("deparser_hex")
        try:
            definition = bytes.fromhex(definition_hex).decode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise RuntimeError(
                f"read relation deparser output is missing: {relation_name}"
            ) from None

        spec = READ_RELATION_SPECS[relation_name]
        owner_expected = (
            expected_owner.get(relation_name)
            if isinstance(expected_owner, dict)
            else expected_owner
        )
        columns = observation.get("columns")
        expected_columns = [
            {"name": name, "type": type_name} for name, type_name in spec["columns"]
        ]
        owner = observation.get("owner")
        if (
            observation.get("relkind") != "v"
            or observation.get("reloptions") != ["security_barrier=true"]
            or columns != expected_columns
            or not isinstance(owner, str)
            or (owner_expected is not None and owner != owner_expected)
        ):
            raise RuntimeError(
                f"read relation metadata differs from its fixed contract: {relation_name}"
            )
        normalized[relation_name] = {
            "pg_get_viewdef_sha256": hashlib.sha256(
                definition.encode("utf-8")
            ).hexdigest(),
            "definition_source": "pg_catalog.pg_get_viewdef(oid, true)",
            "metadata": {
                "relkind": observation["relkind"],
                "owner": owner,
                "reloptions": observation["reloptions"],
                "columns": columns,
            },
        }

    if set(normalized) != set(READ_RELATION_SPECS):
        raise RuntimeError("read relation catalog evidence is incomplete")
    return normalized


def _read_relation_structure(snapshot: dict[str, dict]) -> dict[str, dict]:
    return {
        relation_name: {
            "pg_get_viewdef_sha256": evidence["pg_get_viewdef_sha256"],
            "relkind": evidence["metadata"]["relkind"],
            "reloptions": evidence["metadata"]["reloptions"],
            "columns": evidence["metadata"]["columns"],
        }
        for relation_name, evidence in snapshot.items()
    }


def _assert_read_relation_structure_matches(
    reference: dict[str, dict], observed: dict[str, dict], *, phase: str
) -> None:
    if _read_relation_structure(reference) != _read_relation_structure(observed):
        raise RuntimeError(f"read relation deparser/metadata changed during {phase}")


def _read_relation_sql_input_sha256() -> dict[str, dict[str, str]]:
    return {
        relation_name: {
            "source": spec["sql"],
            "sha256": hashlib.sha256(
                (ROOT / "apps/api/alembic/sql" / spec["sql"]).read_bytes()
            ).hexdigest(),
        }
        for relation_name, spec in READ_RELATION_SPECS.items()
    }


def _installed_read_relation_evidence(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
    expected_owner: str | dict[str, str],
) -> dict[str, dict]:
    raw = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=f"SELECT {_read_relation_evidence_expression()}::text",
    ).strip()
    return _normalize_read_relation_observations(raw, expected_owner=expected_owner)


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
    identity_api_consumer_password: str,
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
            f"CREATE ROLE {IDENTITY_API_CONSUMER_ROLE} LOGIN "
            f"PASSWORD '{identity_api_consumer_password}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
            "NOREPLICATION NOBYPASSRLS;\n"
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
    preference_password: str,
    notification_append_password: str,
    notification_inbox_password: str,
    notification_delivery_read_password: str,
    audit_append_password: str,
    audit_query_password: str,
    notification_read_password: str,
    audit_read_password: str,
    audit_daily_refresh_password: str,
    audit_maintenance_password: str,
    jobs_notification_scope_password: str,
    jobs_list_query_password: str,
    jobs_lifecycle_password: str,
    jobs_dispatch_relay_password: str,
    jobs_publication_password: str,
    export_execution_password: str,
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
            "CREATE ROLE yoao_append_client LOGIN "
            f"PASSWORD '{append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_cleanup_client LOGIN "
            f"PASSWORD '{cleanup_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_catalog_client LOGIN "
            f"PASSWORD '{catalog_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_source_client LOGIN "
            f"PASSWORD '{source_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_import_client LOGIN "
            f"PASSWORD '{import_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_operator_client LOGIN "
            f"PASSWORD '{operator_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_media_control_client LOGIN "
            f"PASSWORD '{media_control_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_media_worker_client LOGIN "
            f"PASSWORD '{media_worker_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_asset_image_worker_client LOGIN "
            f"PASSWORD '{image_worker_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_labeling_mask_gc_client LOGIN "
            f"PASSWORD '{mask_gc_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_preannotation_result_client LOGIN "
            f"PASSWORD '{result_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_notification_preferences_client LOGIN "
            f"PASSWORD '{preference_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_notification_append_client LOGIN "
            f"PASSWORD '{notification_append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {NOTIFICATION_INBOX_ROLE} LOGIN "
            f"PASSWORD '{notification_inbox_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {NOTIFICATION_DELIVERY_READ_ROLE} LOGIN "
            f"PASSWORD '{notification_delivery_read_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_audit_append_client LOGIN "
            f"PASSWORD '{audit_append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_audit_query_client LOGIN "
            f"PASSWORD '{audit_query_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_notification_read_client LOGIN "
            f"PASSWORD '{notification_read_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE yoao_audit_read_client LOGIN "
            f"PASSWORD '{audit_read_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {AUDIT_DAILY_REFRESH_ROLE} LOGIN "
            f"PASSWORD '{audit_daily_refresh_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {AUDIT_MAINTENANCE_ROLE} LOGIN "
            f"PASSWORD '{audit_maintenance_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {JOBS_NOTIFICATION_SCOPE_ROLE} LOGIN "
            f"PASSWORD '{jobs_notification_scope_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {JOBS_LIST_QUERY_ROLE} LOGIN "
            f"PASSWORD '{jobs_list_query_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {JOBS_LIFECYCLE_ROLE} LOGIN "
            f"PASSWORD '{jobs_lifecycle_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {JOBS_DISPATCH_RELAY_ROLE} LOGIN "
            f"PASSWORD '{jobs_dispatch_relay_password}' NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {JOBS_PUBLICATION_ROLE} LOGIN "
            f"PASSWORD '{jobs_publication_password}' NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {EXPORT_EXECUTION_ROLE} LOGIN "
            f"PASSWORD '{export_execution_password}' NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            'REVOKE TEMPORARY ON DATABASE :"expected_database" FROM PUBLIC;\n'
            'GRANT CONNECT ON DATABASE :"expected_database" TO '
            f'"{NOTIFICATION_READ_ROLE}", "{AUDIT_READ_ROLE}", '
            f'"{AUDIT_DAILY_REFRESH_ROLE}", "{AUDIT_MAINTENANCE_ROLE}", '
            f'"{JOBS_NOTIFICATION_SCOPE_ROLE}";\n'
            'GRANT TEMPORARY ON DATABASE :"expected_database" TO '
            + ", ".join(
                ("legacy_runtime",)
                + tuple(
                    role
                    for role in MODULE_ROLES
                    if role
                    not in {
                        "audit_code",
                        "yoao_asset_operator_client",
                        "yoao_asset_media_control_client",
                        "yoao_asset_media_worker_client",
                        "yoao_asset_image_worker_client",
                        "yoao_labeling_mask_gc_client",
                        "yoao_preannotation_result_client",
                        NOTIFICATION_PREFERENCES_ROLE,
                        NOTIFICATION_APPEND_ROLE,
                        NOTIFICATION_INBOX_ROLE,
                        NOTIFICATION_DELIVERY_READ_ROLE,
                        AUDIT_APPEND_ROLE,
                        AUDIT_QUERY_ROLE,
                        NOTIFICATION_READ_ROLE,
                        AUDIT_READ_ROLE,
                        AUDIT_DAILY_REFRESH_ROLE,
                        AUDIT_MAINTENANCE_ROLE,
                        JOBS_NOTIFICATION_SCOPE_ROLE,
                        JOBS_LIST_QUERY_ROLE,
                        JOBS_LIFECYCLE_ROLE,
                        JOBS_DISPATCH_RELAY_ROLE,
                        JOBS_PUBLICATION_ROLE,
                        EXPORT_EXECUTION_ROLE,
                        VIDEO_EXECUTION_ROLE,
                        INFERENCE_REGISTRY_INGRESS_ROLE,
                        INFERENCE_ROUTING_ROLE,
                        *IDENTITY_CLIENT_ROLES.values(),
                    }
                )
            )
            + ";\n"
        ),
        variables={"expected_database": database},
    )


def _run_alembic(
    backend, action: str, revision: str, *, database: str | None = None
) -> None:
    from sqlalchemy.engine import make_url

    environment = backend.environment()
    if database is not None:
        for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
            environment[key] = (
                make_url(environment[key])
                .set(database=database)
                .render_as_string(hide_password=False)
            )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        cwd=ROOT / "apps/api",
        env=environment,
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
    *,
    migration_role: str,
) -> dict[str, dict]:
    provisioning_id = str(uuid4())
    provisioning_email = f"module-data-{uuid4().hex}@example.invalid"
    provisioning_request = str(uuid4())
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
        "SELECT aap_identity_v1.read_provisioning_account(:'provisioning_email');\n"
        "SELECT aap_identity_v1.create_provisioning_account(jsonb_build_object("
        "'schema','aap-identity-provisioning/v1',"
        "'mode','bootstrap_admin','account_id',:'provisioning_id',"
        "'email',:'provisioning_email','name','Provisioning probe',"
        "'password_hash','probe-only-hash','now','2026-10-21T00:00:00+00:00',"
        "'request_id',:'provisioning_request'));\n"
        "SELECT aap_identity_v1.read_provisioning_account(:'provisioning_email');\n"
        "SELECT aap_identity_v1.lock_accounts_share(ARRAY[]::uuid[], TRUE);\n"
        "SELECT aap_identity_v1.lock_accounts_update(ARRAY[]::uuid[], FALSE);\n"
        "SELECT aap_identity_v1.lock_accounts_key_share(ARRAY[]::uuid[], TRUE);\n"
        "SELECT yoao_identity_briefs_v1.briefs_policy();\n"
        "SELECT yoao_identity_briefs_v1.execute_briefs_command('{}'::jsonb);\n"
        "SELECT aap_identity_deactivation_v1.deactivation_policy();\n"
        "SELECT aap_identity_deactivation_v1.execute_deactivation_command(NULL::jsonb);\n"
        "SELECT aap_identity_deactivation_v1.due_policy();\n"
        "SELECT aap_identity_deactivation_v1.process_due_deactivations(NULL::jsonb);\n"
        "SELECT aap_notifications_v1.identity_delivery_policy();\n"
        "SELECT aap_notifications_v1.delete_command(NULL::pg_catalog.jsonb);\n"
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
        "SELECT yoao_insights_v1.clear_project_task_views(:'project_id'::uuid);\n"
        "SELECT yoao_diagnostics_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_export_v1.clear_project_export_artifacts(:'project_id'::uuid);\n"
        "SELECT aap_jobs_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_learning_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.append_dataset_items("
        ":'dataset_id'::uuid, ARRAY[]::uuid[], :'operation_id'::uuid);\n"
        "SELECT 'AAP_READ_RELATIONS_FROZEN=' || ("
        f"{_read_relation_evidence_expression(frozen=True)}"
        ")::text;\n"
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
            "provisioning_id": provisioning_id,
            "provisioning_email": provisioning_email,
            "provisioning_request": provisioning_request,
        },
    )
    marker = "AAP_READ_RELATIONS_FROZEN="
    relation_rows = [
        line[len(marker) :] for line in output.splitlines() if line.startswith(marker)
    ]
    if len(relation_rows) != 1:
        raise RuntimeError("frozen SQL read relation evidence is missing")
    frozen_relations = _normalize_read_relation_observations(
        relation_rows[0],
        expected_owner=migration_role,
        projected=True,
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
        "aap-notification-delete/v1": "invalid_request",
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
    account_lock_replies = responses.get("aap-identity-account-locks/v1", [])
    if len(account_lock_replies) != 3 or any(
        response.get("accounts") != [] or set(response) != {"schema", "accounts"}
        for response in account_lock_replies
    ):
        raise RuntimeError("native Identity account-lock probes did not succeed")
    provisioning_replies = [
        response
        for response in responses.get("aap-identity-provisioning/v1", [])
        if response.get("email") == provisioning_email
    ]
    if (
        len(provisioning_replies) != 3
        or provisioning_replies[0].get("account_id") is not None
        or provisioning_replies[1].get("account_id") != provisioning_id
        or provisioning_replies[2].get("account_id") != provisioning_id
    ):
        raise RuntimeError(
            "native Identity provisioning lookup/create probes did not succeed"
        )

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
            "AND to_regnamespace('yoao_fixture_v1') IS NULL "
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
            "AND to_regnamespace('yoao_identity_briefs_v1') IS NULL "
            "AND to_regnamespace('aap_labeling_class_query_v1') IS NULL "
            "AND to_regnamespace('yoao_labeling_class_query_v1') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.delete_command(pg_catalog.jsonb)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.append_job_terminal_notification(jsonb)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.read_notification(uuid)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.find_terminal_notification(uuid,text,uuid)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.delivery_job_scopes(uuid[])') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.delivery_member_pairs(jsonb)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.delivery_reviewer_pairs(jsonb)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.delivery_owner_pairs(jsonb)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.delivery_super_pairs(jsonb)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.inbox_total_count(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1.inbox_unread_count(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_notifications_v1._visible_inbox_rows(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_audit_v1.refresh_audit_bi_mv()') IS NULL "
            "AND to_regprocedure('aap_identity_v1.read_provisioning_account(text)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.create_provisioning_account(jsonb)') IS NULL "
            "AND to_regprocedure('aap_audit_v1.record_identity_bootstrap_admin(uuid,text,text)') IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM public.users WHERE id=:'provisioning_id'::uuid) "
            "AND NOT EXISTS (SELECT 1 FROM public.audit_logs "
            "WHERE actor_id=:'provisioning_id'::uuid "
            "AND action='system.bootstrap_admin' "
            "AND request_id=:'provisioning_request') "
            "AND to_regprocedure('aap_workspace_v1.identity_brief_project_roles(uuid,uuid[])') IS NULL "
            "AND to_regnamespace('aap_workspace_membership_v1') IS NULL "
            "AND to_regnamespace('yoao_workspace_membership_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_owner_transfer_v1') IS NULL "
            "AND to_regnamespace('yoao_workspace_owner_transfer_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_template_query_v1') IS NULL "
            "AND to_regnamespace('yoao_workspace_template_query_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_project_query_v1') IS NULL "
            "AND to_regnamespace('yoao_workspace_project_query_v1') IS NULL "
            "AND to_regnamespace('aap_inference_pipelines_v1') IS NULL "
            "AND to_regnamespace('aap_inference_health_observation_v1') IS NULL "
            "AND to_regnamespace('aap_inference_health_data_v1') IS NULL "
            "AND to_regnamespace('aap_inference_registry_ingress_v1') IS NULL "
            "AND to_regnamespace('aap_inference_routing_v1') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.authority_secret') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.registry_state') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.scopes') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.attempts') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.weights') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.controls') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.control_attempts') IS NULL "
            "AND to_regclass('aap_inference_routing_v1.command_receipts') IS NULL "
            "AND to_regprocedure('aap_inference_routing_v1.routing_policy()') IS NULL "
            "AND to_regprocedure('aap_inference_routing_v1.routing_step(text,text)') IS NULL "
            "AND to_regprocedure('aap_inference_routing_v1.scope_retirement_step(text,text)') IS NULL "
            "AND to_regprocedure('aap_inference_routing_v1.video_execution_scope_step(text,text)') IS NULL "
            "AND to_regprocedure('aap_jobs_v1.video_execution_step(text,text)') IS NULL "
            "AND to_regclass('aap_jobs_v1.video_executions') IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
            "WHERE tgname IN ('aap_route_registry_after_insert', 'aap_route_registry_before_change', "
            "'aap_route_pool_before_change', 'aap_route_pool_after_generation', "
            "'aap_route_member_before_change', 'aap_route_project_pool_before_change') "
            "AND NOT tgisinternal) "
            "AND to_regclass('aap_inference_registry_ingress_v1.env_sync_receipts') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.registry_ingress_policy()') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.registry_by_url(text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.registry_candidates(text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.singleton_pool(uuid)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.sync_result(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)') IS NULL "
            "AND to_regnamespace('aap_inference_backend_probe_v1') IS NULL "
            "AND to_regclass('aap_inference_backend_probe_v1.probe_secret') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.clear_primary_backend_bindings(uuid)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.read_import_project(text)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.write_seed_members(jsonb)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.write_seed_project_projection(uuid,jsonb)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.create_seed_persona_account(jsonb)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.replace_fixture_preferences(uuid,jsonb)') IS NULL "
            "AND to_regnamespace('aap_identity_fixture_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_fixture_v1') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.create_scale_seed_project(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.guard_scale_seed_cleanup(text)') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.delete_scale_seed_projects(text,uuid[])') IS NULL "
            "AND to_regprocedure('aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.read_scale_seed_super_admin()') IS NULL "
            "AND to_regprocedure('aap_identity_v1.read_scale_seed_annotator(uuid[])') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)') IS NULL "
            "AND to_regprocedure('aap_learning_v1.registry_evaluation_reference_exists(uuid)') IS NULL "
            "AND to_regprocedure('aap_jobs_v1.count_running_registry_predictions(uuid)') IS NULL "
            "AND to_regnamespace('aap_workspace_directory_query_v1') IS NULL "
            "AND to_regnamespace('yoao_workspace_directory_query_v1') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.workspace_project_query_counters(uuid[])') IS NULL "
            "AND to_regprocedure('aap_assets_v1.workspace_project_query_dataset_ids(uuid)') IS NULL "
            "AND to_regprocedure('aap_jobs_v1.workspace_project_query_latest_job_status(uuid)') IS NULL "
            "AND to_regprocedure('aap_inference_v1.workspace_project_query_pool(uuid)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.workspace_configuration_org_scope(uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.workspace_template_query_org_ids(uuid)') IS NULL "
            "AND to_regprocedure('aap_assets_v1.workspace_configuration_datasets(uuid)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.workspace_configuration_video_facts(uuid)') IS NULL "
            "AND to_regprocedure('aap_inference_v1.enable_workspace_project_backend(uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.read_workspace_membership_accounts(uuid[])') IS NULL "
            "AND to_regprocedure('aap_audit_v1.record_workspace_membership_event(text,jsonb,uuid,jsonb,jsonb)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.read_membership_work(uuid,uuid,uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.handoff_membership_annotation(uuid,uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1._membership_python_repr(jsonb)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.handoff_membership_reviewer(uuid,uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_labeling_v1.release_membership_task_locks(uuid,uuid)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.read_actor_context(uuid,text)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.lock_accounts_share(uuid[],boolean)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.lock_accounts_update(uuid[],boolean)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.lock_accounts_key_share(uuid[],boolean)') IS NULL "
            "AND to_regprocedure('aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])') IS NULL "
            "AND to_regclass('aap_identity_v1.account_profiles') IS NULL "
            "AND to_regclass('aap_identity_v1.invitation_audit_targets') IS NULL "
            "AND to_regprocedure('aap_identity_recovery_v1.read_registration_status(jsonb)') IS NULL "
            "AND to_regnamespace('aap_identity_mail_v2') IS NULL "
            "AND to_regnamespace('aap_workspace_v1') IS NULL "
            "AND to_regnamespace('aap_assets_v1') IS NULL "
            "AND to_regnamespace('aap_labeling_v1') IS NULL "
            "AND to_regnamespace('aap_jobs_v1') IS NULL "
            "AND to_regnamespace('aap_learning_v1') IS NULL "
            "AND to_regnamespace('aap_inference_v1') IS NULL "
            "AND to_regnamespace('aap_insights_v1') IS NULL "
            "AND to_regnamespace('yoao_insights_v1') IS NULL "
            "AND to_regnamespace('aap_diagnostics_v1') IS NULL "
            "AND to_regnamespace('yoao_diagnostics_v1') IS NULL "
            "AND to_regnamespace('aap_export_v1') IS NULL "
            "AND to_regnamespace('aap_audit_v1') IS NULL "
            "AND to_regnamespace('aap_settings_v1') IS NULL "
            "AND to_regnamespace('aap_notifications_v1') IS NULL "
            "AND to_regclass('public.task_append_operations') IS NULL "
            "AND to_regclass('public.identity_account_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_account_presence') IS NULL "
            "AND to_regclass('public.identity_account_preferences') IS NULL "
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
            "AND to_regclass('public.workspace_membership_operation_receipts') IS NULL "
            "AND to_regclass('public.workspace_owner_transfer_operation_receipts') IS NULL "
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
        variables={
            "expected_database": database,
            "provisioning_id": provisioning_id,
            "provisioning_request": provisioning_request,
        },
    )
    if rollback_state.strip() != "t":
        raise RuntimeError("native frozen-SQL transaction did not roll back cleanly")
    return frozen_relations


def _replay_insights_storage_contract(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
) -> dict:
    artifacts = "\n".join(
        _sql_artifact_path(name).read_text(encoding="utf-8")
        for name in INSIGHTS_STORAGE_SQL
    )
    result = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        variables={"expected_database": database},
        sql=f"""
BEGIN;
SELECT 1 / (current_database() = :'expected_database'
    AND (SELECT version_num FROM public.alembic_version) = 'bo0486')::integer;
SELECT 'public.mv_user_perf_daily'::pg_catalog.regclass::oid AS mv_oid \\gset
{artifacts}
SELECT yoao_insights_v1.refresh_user_perf_daily();
SELECT 'insights_contract_installed=' || (
    SELECT c.oid = :'mv_oid'::oid AND c.relispopulated
        AND (SELECT count(*) FROM pg_catalog.pg_index i WHERE i.indrelid = c.oid) = 3
        AND NOT yoao_insights_v1.has_personal_dataset_view(NULL, NULL, NULL)
    FROM pg_catalog.pg_class c
    WHERE c.oid = 'public.mv_user_perf_daily'::pg_catalog.regclass
)::text;
ROLLBACK;
SELECT 'insights_contract_rolled_back=' || (
    pg_catalog.to_regprocedure('yoao_insights_v1.refresh_user_perf_daily()') IS NULL
    AND pg_catalog.to_regprocedure('yoao_insights_v1.list_saved_task_views(uuid,uuid,text)') IS NULL
    AND pg_catalog.to_regprocedure('yoao_insights_v1.has_personal_dataset_view(uuid,uuid,uuid)') IS NULL
    AND 'public.mv_user_perf_daily'::pg_catalog.regclass::oid = :'mv_oid'::oid
)::text;
""",
    )
    markers = {line for line in result.splitlines() if line.startswith("insights_")}
    if markers != {
        "insights_contract_installed=true",
        "insights_contract_rolled_back=true",
    }:
        raise RuntimeError(
            "Insights SQL did not preserve the existing projection and roll back"
        )
    return {
        "base_revision": "bo0486",
        "revision": "bo0487",
        "materialized_view_preserved_and_refreshed": True,
        "rolled_back": True,
    }


def _run_collect_only(
    args: argparse.Namespace,
    test_files: tuple[str, ...],
    output: Path,
    junit: Path,
    collection_manifest: Path,
) -> int:
    """Collect acceptance pytest node IDs without provisioning a Docker runtime."""
    evidence = {
        "schema": 1,
        "acceptance": "module-data-boundary",
        "status": "running",
        "phase": "pytest-collection",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_root": str(ROOT),
        "test_files": list(test_files),
        "collection_manifest": str(collection_manifest),
        "collected_test_nodes": None,
        "test_exit_code": None,
        "cleanup": {"errors": []},
    }
    _write_evidence(output, evidence)
    exit_code = 1
    try:
        selected_test_paths = {target.partition("::")[0] for target in test_files}
        missing_tests = sorted(
            name
            for name in selected_test_paths
            if not (ROOT / "apps/api" / name).is_file()
        )
        if missing_tests:
            raise RuntimeError(f"Missing module-data acceptance cases: {missing_tests}")
        command = [
            sys.executable,
            "-c",
            "import json,os,sys\n"
            "args=sys.argv[1:]\n"
            "os.chdir(args.pop(0))\n"
            "manifest=args.pop(0)\n"
            "sys.path.insert(0, os.getcwd())\n"
            "import pytest\n"
            "class CollectionManifest:\n"
            "    def pytest_collection_finish(self, session):\n"
            "        with open(manifest, 'w', encoding='utf-8') as output:\n"
            "            json.dump([item.nodeid for item in session.items], output)\n"
            "raise SystemExit(pytest.main(args, plugins=[CollectionManifest()]))\n",
            str(ROOT / "apps/api"),
            str(collection_manifest),
            "-q",
            "--tb=short",
            "--collect-only",
            *test_files,
        ]
        env = os.environ.copy()
        env["AAP_MODULE_DATA_ROLE_ACCEPTANCE"] = "1"
        result = subprocess.run(command, env=env, check=False)
        evidence["test_exit_code"] = result.returncode
        if result.returncode == 0:
            collected = json.loads(collection_manifest.read_text(encoding="utf-8"))
            evidence["collected_test_nodes"] = collected
        exit_code = result.returncode
        evidence["status"] = "collected" if exit_code == 0 else "failed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = {
            "phase": "pytest-collection",
            "error_type": type(error).__name__,
        }
    finally:
        if exit_code != 0:
            evidence["status"] = "failed"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_evidence(output, evidence)
        result_label = "COLLECTED" if exit_code == 0 else "FAIL"
        scope_label = "selected subset" if args.test_files else "default selection"
        print(
            f"{result_label}: module-data {scope_label}; "
            f"evidence={output} junit={junit}",
            flush=True,
        )
    return exit_code


def main(
    argv: list[str] | None = None,
    *,
    collect_only: bool = False,
    partition_default: bool = True,
) -> int:
    args = _arguments(argv)
    test_files = (
        tuple(_RECLAMATION_PROFILES)
        if args.reclamation_only
        else tuple(dict.fromkeys(args.test_files or TEST_FILES))
    )
    if partition_default and args.test_files is None and not collect_only:
        return _run_strict_default(args, test_files)
    output = args.output.resolve()
    junit = (
        args.junit_xml.resolve()
        if args.junit_xml
        else output.with_name(f"{output.stem}.junit.xml")
    )
    collection_manifest = output.with_name(f"{output.stem}.collection.json")
    if len({output, junit, collection_manifest}) != 3:
        raise SystemExit(
            "--output, --junit-xml and the collection manifest must be different paths"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    collection_manifest.unlink(missing_ok=True)
    if collect_only:
        return _run_collect_only(args, test_files, output, junit, collection_manifest)

    evidence = {
        "schema": 1,
        "acceptance": "module-data-boundary",
        "status": "running",
        "phase": "preflight",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_root": str(ROOT),
        "container_image": POSTGRES_IMAGE,
        "container_labels": {
            CONTAINER_LABEL: CONTAINER_LABEL_VALUE,
        },
        "contract_artifacts": {
            "sql": [
                *SQL_ARTIFACTS,
                "0475_inbox_retained_job_scope.sql",
                "0479_identity_api_key_usage.sql",
                *CURRENT_NAMESPACE_SQL,
            ],
            "schemas": list(SCHEMA_ARTIFACTS),
        },
        "database": None,
        "roles": list(MODULE_ROLES),
        "identity_api_consumer": None,
        "workspace_node": {"node_prerequisites": "not_checked"},
        "notification_preferences": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "notification_append": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_insert_revoked": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "notification_delivery_read": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "function_only_login": False,
            "api_fact_select_denied": False,
            "node_contract_check": False,
            "lan_function_call": False,
        },
        "notification_inbox": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "function_only_login": False,
            "private_helper_denied": False,
            "acl_guard_probes": None,
            "lan_function_calls": False,
            "node_contract_check": False,
        },
        "notification_terminal_append": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "notification_read_state": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_update_revoked": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "notification_delete": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_delete_revoked": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "notification_read_relations": {
            "node_prerequisites": "not_checked",
            "api_owner_function_grant": False,
            "api_private_reads_revoked": False,
            "api_relation_select_denied": False,
            "api_owner_function_call": False,
            "read_client_grant": False,
            "read_client_function_denied": False,
            "publisher_functions_remain_callable": False,
            "node_relation_check": False,
        },
        "audit_append": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_insert_revoked": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "audit_query": {
            "node_prerequisites": "not_checked",
            "api_function_grant": False,
            "api_function_call": False,
            "function_only_role_grant": False,
            "node_contract_check": False,
        },
        "audit_read_relations": {
            "node_prerequisites": "not_checked",
            "api_relation_grant": False,
            "api_fact_call": False,
            "read_client_grant": False,
            "node_relation_check": False,
        },
        "audit_daily_projection": {"status": "not_checked"},
        "audit_maintenance": {
            "function_only_role_grant": False,
            "direct_partition_writes_denied": False,
            "direct_partition_reads_denied": False,
            "sequence_and_ddl_denied": False,
            "owner_and_future_acl_verified": False,
        },
        "jobs_notification_scope": {
            "node_prerequisites": "not_checked",
            "api_relation_grant": False,
            "api_fact_call": False,
            "read_client_grant": False,
            "node_relation_check": False,
        },
        "jobs_list_query": {"function_only_grants": False},
        "test_files": list(test_files),
        "junit_xml": str(junit),
        "collection_manifest": str(collection_manifest),
        "test_exit_code": None,
        "migrations": {
            "head_before_frozen_install": None,
            "installed_function_body_sha256": {},
            "read_relation_hash_method": {
                "view_definition": "SHA-256 of UTF-8 pg_catalog.pg_get_viewdef(oid, true) output",
                "migration_sql": "SHA-256 of exact migration SQL file bytes; not compared to the deparser digest",
            },
            "read_relation_sql_input_sha256": {},
            "read_relations_before_frozen_install": None,
            "read_relations_frozen_sql_transaction": None,
            "read_relations_after_upgrade": None,
            "read_relations_after_owner_grants": None,
            "base_revision": "0267",
            "frozen_database": None,
            "fresh_main_base_prepared": False,
            "frozen_sql_installed_and_rolled_back": False,
            "upgraded_to_head": False,
            "head_after_upgrade": None,
            "runtime_grants_reapplied": False,
        },
        "native_provider": {"status": "not_selected"},
        "cleanup": {
            "storage_identity_removed": False,
            "catalog_storage_identity_removed": False,
            "source_storage_identity_removed": False,
            "import_storage_identity_removed": False,
            "import_source_storage_identity_removed": False,
            "media_control_storage_identity_removed": False,
            "media_worker_storage_identity_removed": False,
            "image_worker_storage_identity_removed": False,
            "native_source_storage_identity_removed": False,
            "native_connection_storage_identity_removed": False,
            "native_provider_retired": None,
            "native_provider_owner_manifest": None,
            "native_provider_retention_reason": None,
            "native_contract_owner_manifest": None,
            "native_contract_report": None,
            "api_bootstrap_storage_identity_removed": None,
            "audit_archive_writer_storage_identity_removed": None,
            "audit_archive_reader_storage_identity_removed": None,
            "audit_archive_platform_storage_identity_removed": None,
            "mask_content_storage_identity_removed": None,
            "mask_cleanup_storage_identity_removed": None,
            "local_resources_destroyed": False,
            "postgres_container_removed": False,
            "resource_root_retained": None,
            "errors": [],
        },
    }
    _write_evidence(output, evidence)
    phase = "preflight"
    node_diagnostic = None
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
    native_source_storage = None
    native_connection_storage = None
    native_lifecycle_profile = False
    native_provider_owner = None
    native_provider_state = None
    native_provider_scopes = None
    native_process_environment_path = None
    native_process_environment_sha256 = ""
    image_import_storage = None
    api_bootstrap_storage = None
    audit_archive_writer_storage = None
    audit_archive_reader_storage = None
    audit_archive_platform_storage = None
    mask_content_storage = None
    mask_cleanup_storage = None
    resource_root = Path(
        tempfile.mkdtemp(prefix="aap-module-data-acceptance-")
    ).resolve()

    try:
        selected_test_paths = {name.split("::", 1)[0] for name in test_files}
        m2b_profile = args.workspace_consumer_profile == "default" and all(
            _is_m2b_default_target(target) for target in test_files
        )
        saved_source_profile = bool(selected_test_paths & _SAVED_SOURCE_FILES) or any(
            target.partition("[")[0] in _SAVED_SOURCE_NODES for target in test_files
        )
        source_signatures = ()
        if m2b_profile or saved_source_profile:
            from yoao_assets_source_runtime.saved_io import _registry

            source_signatures = tuple(_registry().signatures.values())
        migration_control_profile = any(
            target.partition("[")[0] == _SAVED_MIGRATION_NODE for target in test_files
        )
        native_lifecycle_profile = (
            not collect_only
            and "tests/test_native_pyramid_lifecycle_live.py" in selected_test_paths
        )
        if native_lifecycle_profile and len(selected_test_paths) != 1:
            raise RuntimeError("Native lifecycle requires an exclusive runtime profile")
        physical_retirement_profiles = {
            "tests/test_image_import_identity_profile.py",
            "tests/test_identity_seed_selection_live.py",
        }
        physical_retirement_profile = bool(
            selected_test_paths & physical_retirement_profiles
        )
        if (
            not collect_only
            and physical_retirement_profile
            and len(selected_test_paths) != 1
        ):
            raise RuntimeError(
                "Managed-original retirement requires an exclusive runtime profile"
            )
        missing_tests = sorted(
            name
            for name in selected_test_paths
            if not (ROOT / "apps/api" / name).is_file()
        )
        if missing_tests:
            raise RuntimeError(f"Missing module-data acceptance cases: {missing_tests}")
        node_bin = shutil.which("node")
        workspace_node_dir = ROOT / "apps/workspace/conformance/node"
        if node_bin is None or not (workspace_node_dir / "package.json").is_file():
            evidence["workspace_node"]["node_prerequisites"] = "unavailable"
            raise RuntimeError("Node Workspace contract peer is unavailable")
        phase = "preflight-node-workspace-pg"
        workspace_node_check = subprocess.run(
            [
                node_bin,
                "-e",
                'const fs = require("node:fs"), path = require("node:path");'
                "const root = fs.realpathSync(process.argv[1]);"
                'const pg = fs.realpathSync(require.resolve("pg", { paths: [root] }));'
                'if (!pg.startsWith(root + path.sep + "node_modules" + path.sep)) '
                "process.exit(1)",
                str(workspace_node_dir),
            ],
            cwd=workspace_node_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if workspace_node_check.returncode:
            evidence["workspace_node"]["node_prerequisites"] = "unavailable"
            raise RuntimeError(
                "Workspace Node pg is unavailable; install its locked dependencies "
                "using apps/workspace/conformance/node/README.md"
            )
        evidence["workspace_node"]["node_prerequisites"] = "passed"
        node_provider = ROOT / "apps/notifications/conformance/node/provider.mjs"
        append_node_provider = (
            ROOT / "apps/notifications/conformance/node/append_provider.mjs"
        )
        terminal_append_node_provider = (
            ROOT / "apps/notifications/conformance/node/terminal_append_provider.mjs"
        )
        inbox_node_provider = (
            ROOT / "apps/notifications/conformance/node/inbox_query_provider.mjs"
        )
        delivery_read_node_provider = (
            ROOT / "apps/notifications/conformance/node/delivery_read_provider.mjs"
        )
        read_state_node_provider = (
            ROOT / "apps/notifications/conformance/node/read_state_provider.mjs"
        )
        delete_node_provider = (
            ROOT / "apps/notifications/conformance/node/delete_provider.mjs"
        )
        notification_read_node_provider = (
            ROOT / "apps/notifications/conformance/node/read_relations_provider.mjs"
        )
        jobs_scope_node_provider = (
            ROOT
            / "apps/notifications/conformance/node/jobs_notification_scope_provider.mjs"
        )
        audit_append_node_provider = ROOT / "apps/audit/conformance/node/provider.mjs"
        audit_query_node_provider = (
            ROOT / "apps/audit/conformance/node/query_provider.mjs"
        )
        audit_read_node_provider = (
            ROOT / "apps/audit/conformance/node/read_provider.mjs"
        )
        if (
            node_bin is None
            or not node_provider.is_file()
            or not append_node_provider.is_file()
            or not terminal_append_node_provider.is_file()
            or not inbox_node_provider.is_file()
            or not delivery_read_node_provider.is_file()
            or not read_state_node_provider.is_file()
            or not delete_node_provider.is_file()
            or not notification_read_node_provider.is_file()
            or not jobs_scope_node_provider.is_file()
            or not audit_append_node_provider.is_file()
            or not audit_query_node_provider.is_file()
            or not audit_read_node_provider.is_file()
        ):
            evidence["notification_preferences"]["node_prerequisites"] = "unavailable"
            evidence["notification_inbox"]["node_prerequisites"] = "unavailable"
            evidence["notification_delivery_read"]["node_prerequisites"] = "unavailable"
            evidence["notification_append"]["node_prerequisites"] = "unavailable"
            evidence["notification_terminal_append"]["node_prerequisites"] = (
                "unavailable"
            )
            evidence["notification_read_state"]["node_prerequisites"] = "unavailable"
            evidence["notification_delete"]["node_prerequisites"] = "unavailable"
            evidence["notification_read_relations"]["node_prerequisites"] = (
                "unavailable"
            )
            evidence["audit_append"]["node_prerequisites"] = "unavailable"
            evidence["audit_query"]["node_prerequisites"] = "unavailable"
            evidence["audit_read_relations"]["node_prerequisites"] = "unavailable"
            evidence["jobs_notification_scope"]["node_prerequisites"] = "unavailable"
            raise RuntimeError("Node Notifications contract peer is unavailable")
        phase = "preflight-node-notification-preferences"
        node_check = subprocess.run(
            [node_bin, str(node_provider), "--self-check"],
            cwd=node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, node_check)
            evidence["notification_preferences"]["node_prerequisites"] = (
                "self_check_failed"
            )
            raise RuntimeError(
                "Notifications peer prerequisites failed; install its locked "
                "dependencies using apps/notifications/conformance/node/README.md"
            )
        phase = "preflight-node-notification-append"
        append_node_check = subprocess.run(
            [node_bin, str(append_node_provider), "--self-check"],
            cwd=append_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if append_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, append_node_check)
            evidence["notification_append"]["node_prerequisites"] = "self_check_failed"
            raise RuntimeError(
                "Notifications append peer prerequisites failed; install its locked "
                "dependencies using apps/notifications/conformance/node/README.md"
            )
        phase = "preflight-node-notification-terminal-append"
        terminal_append_node_check = subprocess.run(
            [node_bin, str(terminal_append_node_provider), "--self-check"],
            cwd=terminal_append_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if terminal_append_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(
                phase, terminal_append_node_check
            )
            evidence["notification_terminal_append"]["node_prerequisites"] = (
                "self_check_failed"
            )
            raise RuntimeError(
                "Notifications terminal append peer prerequisites failed"
            )
        phase = "preflight-node-notification-read-state"
        read_state_node_check = subprocess.run(
            [node_bin, str(read_state_node_provider), "--self-check"],
            cwd=read_state_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if read_state_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, read_state_node_check)
            evidence["notification_read_state"]["node_prerequisites"] = (
                "self_check_failed"
            )
            raise RuntimeError(
                "Notifications read-state peer prerequisites failed; install its locked "
                "dependencies using apps/notifications/conformance/node/README.md"
            )
        phase = "preflight-node-notification-delete"
        delete_node_check = subprocess.run(
            [node_bin, str(delete_node_provider), "--self-check"],
            cwd=delete_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if delete_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, delete_node_check)
            evidence["notification_delete"]["node_prerequisites"] = "self_check_failed"
            raise RuntimeError(
                "Notifications deletion peer prerequisites failed; install its locked "
                "dependencies using apps/notifications/conformance/node/README.md"
            )
        phase = "preflight-node-notification-read-relations"
        notification_read_node_check = subprocess.run(
            [node_bin, str(notification_read_node_provider), "--self-check"],
            cwd=notification_read_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if notification_read_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(
                phase, notification_read_node_check
            )
            evidence["notification_read_relations"]["node_prerequisites"] = (
                "self_check_failed"
            )
            raise RuntimeError(
                "Notifications read-relation peer prerequisites failed; install its locked "
                "dependencies using apps/notifications/conformance/node/README.md"
            )
        phase = "preflight-node-audit-append"
        audit_append_node_check = subprocess.run(
            [node_bin, str(audit_append_node_provider), "--self-check"],
            cwd=audit_append_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if audit_append_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, audit_append_node_check)
            evidence["audit_append"]["node_prerequisites"] = "self_check_failed"
            raise RuntimeError(
                "Audit append peer prerequisites failed; install its locked "
                "dependencies using apps/audit/conformance/node/README.md"
            )
        phase = "preflight-node-audit-query"
        audit_query_node_check = subprocess.run(
            [node_bin, str(audit_query_node_provider), "--self-check"],
            cwd=audit_query_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if audit_query_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, audit_query_node_check)
            evidence["audit_query"]["node_prerequisites"] = "self_check_failed"
            raise RuntimeError(
                "Audit query peer prerequisites failed; install its locked "
                "dependencies using apps/audit/conformance/node/README.md"
            )
        phase = "preflight-node-jobs-notification-scope"
        jobs_scope_node_check = subprocess.run(
            [node_bin, str(jobs_scope_node_provider), "--self-check"],
            cwd=jobs_scope_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if jobs_scope_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, jobs_scope_node_check)
            evidence["jobs_notification_scope"]["node_prerequisites"] = (
                "self_check_failed"
            )
            raise RuntimeError("Jobs notification-scope peer prerequisites failed")
        phase = "preflight-node-notification-inbox"
        inbox_node_check = subprocess.run(
            [node_bin, str(inbox_node_provider), "--self-check"],
            cwd=inbox_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inbox_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, inbox_node_check)
            evidence["notification_inbox"]["node_prerequisites"] = "self_check_failed"
            raise RuntimeError("Notifications inbox peer prerequisites failed")
        evidence["notification_inbox"]["node_prerequisites"] = "passed"
        phase = "preflight-node-notification-delivery-read"
        delivery_read_node_check = subprocess.run(
            [node_bin, str(delivery_read_node_provider), "--self-check"],
            cwd=delivery_read_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if delivery_read_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, delivery_read_node_check)
            evidence["notification_delivery_read"]["node_prerequisites"] = (
                "self_check_failed"
            )
            raise RuntimeError("Notifications delivery-read peer prerequisites failed")
        evidence["notification_delivery_read"]["node_prerequisites"] = "passed"
        phase = "preflight-node-audit-read-relations"
        audit_read_node_check = subprocess.run(
            [node_bin, str(audit_read_node_provider), "--self-check"],
            cwd=audit_read_node_provider.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if audit_read_node_check.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, audit_read_node_check)
            evidence["audit_read_relations"]["node_prerequisites"] = "self_check_failed"
            raise RuntimeError(
                "Audit read-relation peer prerequisites failed; install its locked "
                "dependencies using apps/audit/conformance/node/README.md"
            )
        evidence["notification_preferences"]["node_prerequisites"] = "passed"
        evidence["notification_append"]["node_prerequisites"] = "passed"
        evidence["notification_terminal_append"]["node_prerequisites"] = "passed"
        evidence["notification_read_state"]["node_prerequisites"] = "passed"
        evidence["notification_delete"]["node_prerequisites"] = "passed"
        evidence["notification_read_relations"]["node_prerequisites"] = "passed"
        evidence["audit_append"]["node_prerequisites"] = "passed"
        evidence["audit_query"]["node_prerequisites"] = "passed"
        evidence["audit_read_relations"]["node_prerequisites"] = "passed"
        evidence["jobs_notification_scope"]["node_prerequisites"] = "passed"
        grants_path = ROOT / "infra/docker/module-data-grants.sql"
        catalog_grants_path = (ROOT / "infra/docker/assets-grants.sql", "catalog")
        source_grants_path = (ROOT / "infra/docker/assets-grants.sql", "source")
        import_grants_path = (ROOT / "infra/docker/assets-grants.sql", "import")
        operator_grants_path = (ROOT / "infra/docker/assets-grants.sql", "operator")
        media_grants_path = (ROOT / "infra/docker/assets-grants.sql", "media")
        image_grants_path = (ROOT / "infra/docker/assets-grants.sql", "image")
        mask_gc_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "mask_gc_data",
        )
        result_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "preannotation_result_data",
        )
        identity_grants_path = (ROOT / "infra/docker/identity-grants.sql", "auth")
        identity_actor_status_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "actor_status",
        )
        identity_key_grants_path = (ROOT / "infra/docker/identity-grants.sql", "keys")
        identity_session_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "sessions",
        )
        identity_account_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "accounts",
        )
        identity_group_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "groups",
        )
        identity_invitation_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "invitations",
        )
        identity_directory_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "directory",
        )
        identity_admin_password_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "admin_passwords",
        )
        identity_platform_role_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "platform_roles",
        )
        identity_suspension_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "suspensions",
        )
        identity_deletion_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "deletions",
        )
        identity_offboarding_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "offboarding",
        )
        identity_briefs_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "briefs",
        )
        identity_provisioning_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "provisioning",
        )
        workspace_membership_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "membership",
        )
        workspace_owner_transfer_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "owner_transfer",
        )
        workspace_project_maintenance_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "project_maintenance",
        )
        workspace_project_deletion_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "project_deletion",
        )
        workspace_dataset_unlink_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "dataset_unlink",
        )
        workspace_configuration_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "configuration",
        )
        workspace_template_query_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "template_query",
        )
        workspace_project_query_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "project_query",
        )
        workspace_directory_query_grants_path = (
            ROOT / "infra/docker/workspace-grants.sql",
            "directory_query",
        )
        labeling_class_query_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "class_query",
        )
        labeling_video_writer_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "video_writer",
        )
        labeling_task_control_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "task_control",
        )
        labeling_annotation_writes_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "annotation_writes",
        )
        labeling_editing_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "editing",
        )
        labeling_batch_control_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "batch_control",
        )
        labeling_prediction_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "prediction",
        )
        labeling_scene_track_commands_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "scene_track_commands",
        )
        labeling_cross_frame_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "cross_frame",
        )
        labeling_annotation_maintenance_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "annotation_maintenance",
        )
        labeling_discussion_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "discussion",
        )
        labeling_mask_edit_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "mask_edit",
        )
        labeling_scene_read_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "scene_read",
        )
        labeling_video_control_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "video_control",
        )
        labeling_annotation_transfer_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "annotation_transfer",
        )
        labeling_core_read_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "core_read",
        )
        labeling_mask_quality_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "mask_quality",
        )
        labeling_spatial_quality_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "spatial_quality",
        )
        labeling_task_maintenance_grants_path = (
            ROOT / "infra/docker/labeling-grants.sql",
            "task_maintenance",
        )
        inference_pool_reference_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "pool_reference",
        )
        video_restore_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "video_restore",
        )
        inference_pipelines_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "pipelines",
        )
        inference_backend_probe_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "backend_probe",
        )
        inference_health_observation_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "health_observation",
        )
        inference_health_origin_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "health_origin",
        )
        inference_health_data_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "health_data",
        )
        inference_registry_ingress_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "registry_ingress",
        )
        inference_routing_grants_path = (
            ROOT / "infra/docker/inference-grants.sql",
            "routing",
        )
        video_execution_grants_path = (
            ROOT / "infra/docker/jobs-grants.sql",
            "video_execution",
        )
        identity_deactivation_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "deactivation",
        )
        notification_preferences_grants_path = (
            ROOT / "infra/docker/notifications-grants.sql",
            "preferences",
        )
        notification_append_grants_path = (
            ROOT / "infra/docker/notifications-grants.sql",
            "append",
        )
        notification_inbox_grants_path = (
            ROOT / "infra/docker/notifications-grants.sql",
            "inbox",
        )
        notification_delivery_read_grants_path = (
            ROOT / "infra/docker/notifications-grants.sql",
            "delivery_read",
        )
        notification_read_grants_path = (
            ROOT / "infra/docker/notifications-grants.sql",
            "read",
        )
        audit_append_grants_path = (ROOT / "infra/docker/audit-grants.sql", "append")
        audit_query_grants_path = (ROOT / "infra/docker/audit-grants.sql", "query")
        audit_read_grants_path = (ROOT / "infra/docker/audit-grants.sql", "read")
        audit_maintenance_grants_path = (
            ROOT / "infra/docker/audit-grants.sql",
            "maintenance",
        )
        jobs_scope_grants_path = (
            ROOT / "infra/docker/jobs-grants.sql",
            "notification_scope",
        )
        jobs_list_query_grants_path = (
            ROOT / "infra/docker/jobs-grants.sql",
            "list_query",
        )
        jobs_lifecycle_grants_path = (
            ROOT / "infra/docker/jobs-grants.sql",
            "lifecycle",
        )
        jobs_dispatch_grants_path = (ROOT / "infra/docker/jobs-grants.sql", "dispatch")
        execution_grants_path = (
            ROOT / "infra/docker/platform-grants.sql",
            "module_execution",
        )
        identity_recovery_grants_path = (
            ROOT / "infra/docker/identity-grants.sql",
            "recovery",
        )
        lan_production_grants_path = ROOT / "infra/docker/lan-production-grants.sql"
        identity_consumer_grants_path = (
            ROOT / "infra/docker/identity-consumer-grants.sql"
        )
        phase = "preflight-contract-artifacts"
        artifacts = tuple(_sql_artifact_path(name) for name in SQL_ARTIFACTS)
        contract_sql_paths = tuple(
            _sql_artifact_path(name) for name in evidence["contract_artifacts"]["sql"]
        )
        assets_source_schema_paths = tuple(
            ROOT / "apps/assets-source/src/yoao_assets_source" / name
            for name in SCHEMA_ARTIFACTS
            if name in ASSETS_SOURCE_SCHEMA_ARTIFACTS
        )
        bundled_schema_names = tuple(
            name
            for name in SCHEMA_ARTIFACTS
            if name not in ASSETS_SOURCE_SCHEMA_ARTIFACTS
        )
        if (
            not grants_path.is_file()
            or not catalog_grants_path[0].is_file()
            or not source_grants_path[0].is_file()
            or not import_grants_path[0].is_file()
            or not operator_grants_path[0].is_file()
            or not media_grants_path[0].is_file()
            or not image_grants_path[0].is_file()
            or not mask_gc_grants_path[0].is_file()
            or not result_grants_path[0].is_file()
            or not identity_grants_path[0].is_file()
            or not identity_actor_status_grants_path[0].is_file()
            or not identity_key_grants_path[0].is_file()
            or not identity_session_grants_path[0].is_file()
            or not identity_deactivation_grants_path[0].is_file()
            or not notification_preferences_grants_path[0].is_file()
            or not notification_append_grants_path[0].is_file()
            or not notification_inbox_grants_path[0].is_file()
            or not notification_delivery_read_grants_path[0].is_file()
            or not notification_read_grants_path[0].is_file()
            or not audit_append_grants_path[0].is_file()
            or not audit_query_grants_path[0].is_file()
            or not audit_read_grants_path[0].is_file()
            or not audit_maintenance_grants_path[0].is_file()
            or not jobs_scope_grants_path[0].is_file()
            or not jobs_list_query_grants_path[0].is_file()
            or not jobs_lifecycle_grants_path[0].is_file()
            or not jobs_dispatch_grants_path[0].is_file()
            or not execution_grants_path[0].is_file()
            or not identity_group_grants_path[0].is_file()
            or not identity_invitation_grants_path[0].is_file()
            or not identity_directory_grants_path[0].is_file()
            or not identity_admin_password_grants_path[0].is_file()
            or not identity_platform_role_grants_path[0].is_file()
            or not identity_suspension_grants_path[0].is_file()
            or not identity_deletion_grants_path[0].is_file()
            or not identity_offboarding_grants_path[0].is_file()
            or not identity_briefs_grants_path[0].is_file()
            or not identity_provisioning_grants_path[0].is_file()
            or not workspace_membership_grants_path[0].is_file()
            or not workspace_owner_transfer_grants_path[0].is_file()
            or not workspace_project_maintenance_grants_path[0].is_file()
            or not workspace_project_deletion_grants_path[0].is_file()
            or not workspace_dataset_unlink_grants_path[0].is_file()
            or not workspace_configuration_grants_path[0].is_file()
            or not workspace_template_query_grants_path[0].is_file()
            or not workspace_project_query_grants_path[0].is_file()
            or not workspace_directory_query_grants_path[0].is_file()
            or not labeling_class_query_grants_path[0].is_file()
            or not labeling_video_writer_grants_path[0].is_file()
            or not labeling_task_control_grants_path[0].is_file()
            or not labeling_annotation_writes_grants_path[0].is_file()
            or not labeling_editing_grants_path[0].is_file()
            or not labeling_batch_control_grants_path[0].is_file()
            or not labeling_prediction_grants_path[0].is_file()
            or not labeling_scene_track_commands_grants_path[0].is_file()
            or not labeling_cross_frame_grants_path[0].is_file()
            or not labeling_annotation_maintenance_grants_path[0].is_file()
            or not labeling_discussion_grants_path[0].is_file()
            or not labeling_mask_edit_grants_path[0].is_file()
            or not labeling_scene_read_grants_path[0].is_file()
            or not labeling_video_control_grants_path[0].is_file()
            or not labeling_annotation_transfer_grants_path[0].is_file()
            or not labeling_core_read_grants_path[0].is_file()
            or not labeling_mask_quality_grants_path[0].is_file()
            or not labeling_spatial_quality_grants_path[0].is_file()
            or not labeling_task_maintenance_grants_path[0].is_file()
            or not video_restore_grants_path[0].is_file()
            or not inference_pipelines_grants_path[0].is_file()
            or not inference_backend_probe_grants_path[0].is_file()
            or not inference_health_observation_grants_path[0].is_file()
            or not inference_health_origin_grants_path[0].is_file()
            or not inference_health_data_grants_path[0].is_file()
            or not inference_pool_reference_grants_path[0].is_file()
            or not inference_registry_ingress_grants_path[0].is_file()
            or not inference_routing_grants_path[0].is_file()
            or not video_execution_grants_path[0].is_file()
            or not identity_account_grants_path[0].is_file()
            or not identity_recovery_grants_path[0].is_file()
            or not lan_production_grants_path.is_file()
            or not identity_consumer_grants_path.is_file()
            or any(not path.is_file() for path in contract_sql_paths)
            or any(not path.is_file() for path in assets_source_schema_paths)
        ):
            raise RuntimeError(
                "module-data SQL/schema artifacts are missing from this checkout"
            )
        schema_hashes: dict[str, str] = {}
        for path in assets_source_schema_paths:
            contract = json.loads(path.read_text(encoding="utf-8"))
            if (
                contract.get("$schema")
                != "https://json-schema.org/draft/2020-12/schema"
                or not isinstance(contract.get("$id"), str)
                or not isinstance(contract.get("$defs", {}), dict)
            ):
                raise RuntimeError(f"invalid frozen module-data schema: {path.name}")
            schema_hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        for name in bundled_schema_names:
            try:
                contract = load_contract_schema(name)
            except FileNotFoundError as error:
                raise RuntimeError(
                    f"module-data SQL/schema artifacts are missing from this checkout: {name}"
                ) from error
            if (
                contract.get("$schema")
                != "https://json-schema.org/draft/2020-12/schema"
                or not isinstance(contract.get("$id"), str)
                or not isinstance(contract.get("$defs", {}), dict)
            ):
                raise RuntimeError(f"invalid frozen module-data schema: {name}")
            schema_hashes[f"apps/api/alembic/sql/{name}"] = contract_schema_sha256(name)
        evidence["contract_artifacts"]["input_sha256"] = {
            **{
                path.relative_to(ROOT).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in contract_sql_paths
            },
            **schema_hashes,
        }
        grant_sql = read_psql_grant_script(grants_path)
        catalog_grant_sql = read_psql_grant_script(
            catalog_grants_path[0], capability=catalog_grants_path[1]
        )
        source_grant_sql = read_psql_grant_script(
            source_grants_path[0], capability=source_grants_path[1]
        )
        import_grant_sql = read_psql_grant_script(
            import_grants_path[0], capability=import_grants_path[1]
        )
        operator_grant_sql = read_psql_grant_script(
            operator_grants_path[0], capability=operator_grants_path[1]
        )
        media_grant_sql = read_psql_grant_script(
            media_grants_path[0], capability=media_grants_path[1]
        )
        image_grant_sql = read_psql_grant_script(
            image_grants_path[0], capability=image_grants_path[1]
        )
        mask_gc_grant_sql = read_psql_grant_script(
            mask_gc_grants_path[0], capability=mask_gc_grants_path[1]
        )
        result_grant_sql = read_psql_grant_script(
            result_grants_path[0], capability=result_grants_path[1]
        )
        identity_grant_sql = read_psql_grant_script(
            identity_grants_path[0], capability=identity_grants_path[1]
        )
        identity_actor_status_grant_sql = read_psql_grant_script(
            identity_actor_status_grants_path[0],
            capability=identity_actor_status_grants_path[1],
        )
        identity_key_grant_sql = read_psql_grant_script(
            identity_key_grants_path[0], capability=identity_key_grants_path[1]
        )
        identity_session_grant_sql = read_psql_grant_script(
            identity_session_grants_path[0], capability=identity_session_grants_path[1]
        )
        identity_invitation_grant_sql = read_psql_grant_script(
            identity_invitation_grants_path[0],
            capability=identity_invitation_grants_path[1],
        )
        identity_directory_grant_sql = read_psql_grant_script(
            identity_directory_grants_path[0],
            capability=identity_directory_grants_path[1],
        )
        identity_admin_password_grant_sql = read_psql_grant_script(
            identity_admin_password_grants_path[0],
            capability=identity_admin_password_grants_path[1],
        )
        identity_platform_role_grant_sql = read_psql_grant_script(
            identity_platform_role_grants_path[0],
            capability=identity_platform_role_grants_path[1],
        )
        identity_suspension_grant_sql = read_psql_grant_script(
            identity_suspension_grants_path[0],
            capability=identity_suspension_grants_path[1],
        )
        identity_deletion_grant_sql = read_psql_grant_script(
            identity_deletion_grants_path[0],
            capability=identity_deletion_grants_path[1],
        )
        identity_offboarding_grant_sql = read_psql_grant_script(
            identity_offboarding_grants_path[0],
            capability=identity_offboarding_grants_path[1],
        )
        identity_briefs_grant_sql = read_psql_grant_script(
            identity_briefs_grants_path[0], capability=identity_briefs_grants_path[1]
        )
        identity_provisioning_grant_sql = read_psql_grant_script(
            identity_provisioning_grants_path[0],
            capability=identity_provisioning_grants_path[1],
        )
        workspace_membership_grant_sql = read_psql_grant_script(
            workspace_membership_grants_path[0],
            capability=workspace_membership_grants_path[1],
        )
        workspace_owner_transfer_grant_sql = read_psql_grant_script(
            workspace_owner_transfer_grants_path[0],
            capability=workspace_owner_transfer_grants_path[1],
        )
        workspace_project_maintenance_grant_sql = read_psql_grant_script(
            workspace_project_maintenance_grants_path[0],
            capability=workspace_project_maintenance_grants_path[1],
        )
        workspace_project_deletion_grant_sql = read_psql_grant_script(
            workspace_project_deletion_grants_path[0],
            capability=workspace_project_deletion_grants_path[1],
        )
        workspace_dataset_unlink_grant_sql = read_psql_grant_script(
            workspace_dataset_unlink_grants_path[0],
            capability=workspace_dataset_unlink_grants_path[1],
        )
        workspace_configuration_grant_sql = read_psql_grant_script(
            workspace_configuration_grants_path[0],
            capability=workspace_configuration_grants_path[1],
        )
        workspace_template_query_grant_sql = read_psql_grant_script(
            workspace_template_query_grants_path[0],
            capability=workspace_template_query_grants_path[1],
        )
        workspace_directory_query_grant_sql = read_psql_grant_script(
            workspace_directory_query_grants_path[0],
            capability=workspace_directory_query_grants_path[1],
        )
        labeling_class_query_grant_sql = read_psql_grant_script(
            labeling_class_query_grants_path[0],
            capability=labeling_class_query_grants_path[1],
        )
        labeling_video_writer_grant_sql = read_psql_grant_script(
            labeling_video_writer_grants_path[0],
            capability=labeling_video_writer_grants_path[1],
        )
        labeling_task_control_grant_sql = read_psql_grant_script(
            labeling_task_control_grants_path[0],
            capability=labeling_task_control_grants_path[1],
        )
        labeling_annotation_writes_grant_sql = read_psql_grant_script(
            labeling_annotation_writes_grants_path[0],
            capability=labeling_annotation_writes_grants_path[1],
        )
        labeling_editing_grant_sql = read_psql_grant_script(
            labeling_editing_grants_path[0], capability=labeling_editing_grants_path[1]
        )
        labeling_batch_control_grant_sql = read_psql_grant_script(
            labeling_batch_control_grants_path[0],
            capability=labeling_batch_control_grants_path[1],
        )
        labeling_prediction_grant_sql = read_psql_grant_script(
            labeling_prediction_grants_path[0],
            capability=labeling_prediction_grants_path[1],
        )
        labeling_scene_track_commands_grant_sql = read_psql_grant_script(
            labeling_scene_track_commands_grants_path[0],
            capability=labeling_scene_track_commands_grants_path[1],
        )
        labeling_cross_frame_grant_sql = read_psql_grant_script(
            labeling_cross_frame_grants_path[0],
            capability=labeling_cross_frame_grants_path[1],
        )
        labeling_annotation_maintenance_grant_sql = read_psql_grant_script(
            labeling_annotation_maintenance_grants_path[0],
            capability=labeling_annotation_maintenance_grants_path[1],
        )
        labeling_discussion_grant_sql = read_psql_grant_script(
            labeling_discussion_grants_path[0],
            capability=labeling_discussion_grants_path[1],
        )
        labeling_mask_edit_grant_sql = read_psql_grant_script(
            labeling_mask_edit_grants_path[0],
            capability=labeling_mask_edit_grants_path[1],
        )
        labeling_scene_read_grant_sql = read_psql_grant_script(
            labeling_scene_read_grants_path[0],
            capability=labeling_scene_read_grants_path[1],
        )
        labeling_video_control_grant_sql = read_psql_grant_script(
            labeling_video_control_grants_path[0],
            capability=labeling_video_control_grants_path[1],
        )
        labeling_annotation_transfer_grant_sql = read_psql_grant_script(
            labeling_annotation_transfer_grants_path[0],
            capability=labeling_annotation_transfer_grants_path[1],
        )
        labeling_core_read_grant_sql = read_psql_grant_script(
            labeling_core_read_grants_path[0],
            capability=labeling_core_read_grants_path[1],
        )
        labeling_mask_quality_grant_sql = read_psql_grant_script(
            labeling_mask_quality_grants_path[0],
            capability=labeling_mask_quality_grants_path[1],
        )
        labeling_spatial_quality_grant_sql = read_psql_grant_script(
            labeling_spatial_quality_grants_path[0],
            capability=labeling_spatial_quality_grants_path[1],
        )
        labeling_task_maintenance_grant_sql = read_psql_grant_script(
            labeling_task_maintenance_grants_path[0],
            capability=labeling_task_maintenance_grants_path[1],
        )
        inference_pool_reference_grant_sql = read_psql_grant_script(
            inference_pool_reference_grants_path[0],
            capability=inference_pool_reference_grants_path[1],
        )
        video_restore_grant_sql = read_psql_grant_script(
            video_restore_grants_path[0], capability=video_restore_grants_path[1]
        )
        inference_pipelines_grant_sql = read_psql_grant_script(
            inference_pipelines_grants_path[0],
            capability=inference_pipelines_grants_path[1],
        )
        inference_backend_probe_grant_sql = read_psql_grant_script(
            inference_backend_probe_grants_path[0],
            capability=inference_backend_probe_grants_path[1],
        )
        inference_health_observation_grant_sql = read_psql_grant_script(
            inference_health_observation_grants_path[0],
            capability=inference_health_observation_grants_path[1],
        )
        inference_health_origin_grant_sql = read_psql_grant_script(
            inference_health_origin_grants_path[0],
            capability=inference_health_origin_grants_path[1],
        )
        inference_health_data_grant_sql = read_psql_grant_script(
            inference_health_data_grants_path[0],
            capability=inference_health_data_grants_path[1],
        )
        inference_registry_ingress_grant_sql = read_psql_grant_script(
            inference_registry_ingress_grants_path[0],
            capability=inference_registry_ingress_grants_path[1],
        )
        inference_routing_grant_sql = read_psql_grant_script(
            inference_routing_grants_path[0],
            capability=inference_routing_grants_path[1],
        )
        video_execution_grant_sql = read_psql_grant_script(
            video_execution_grants_path[0], capability=video_execution_grants_path[1]
        )
        workspace_project_query_grant_sql = read_psql_grant_script(
            workspace_project_query_grants_path[0],
            capability=workspace_project_query_grants_path[1],
        )
        identity_group_grant_sql = read_psql_grant_script(
            identity_group_grants_path[0], capability=identity_group_grants_path[1]
        )
        identity_deactivation_grant_sql = read_psql_grant_script(
            identity_deactivation_grants_path[0],
            capability=identity_deactivation_grants_path[1],
        )
        notification_preferences_grant_sql = read_psql_grant_script(
            notification_preferences_grants_path[0],
            capability=notification_preferences_grants_path[1],
        )
        notification_append_grant_sql = read_psql_grant_script(
            notification_append_grants_path[0],
            capability=notification_append_grants_path[1],
        )
        notification_inbox_grant_sql = read_psql_grant_script(
            notification_inbox_grants_path[0],
            capability=notification_inbox_grants_path[1],
        )
        notification_delivery_read_grant_sql = read_psql_grant_script(
            notification_delivery_read_grants_path[0],
            capability=notification_delivery_read_grants_path[1],
        )
        notification_read_grant_sql = read_psql_grant_script(
            notification_read_grants_path[0],
            capability=notification_read_grants_path[1],
        )
        audit_append_grant_sql = read_psql_grant_script(
            audit_append_grants_path[0], capability=audit_append_grants_path[1]
        )
        audit_query_grant_sql = read_psql_grant_script(
            audit_query_grants_path[0], capability=audit_query_grants_path[1]
        )
        audit_read_grant_sql = read_psql_grant_script(
            audit_read_grants_path[0], capability=audit_read_grants_path[1]
        )
        audit_maintenance_grant_sql = read_psql_grant_script(
            audit_maintenance_grants_path[0],
            capability=audit_maintenance_grants_path[1],
        )
        jobs_scope_grant_sql = read_psql_grant_script(
            jobs_scope_grants_path[0], capability=jobs_scope_grants_path[1]
        )
        identity_account_grant_sql = read_psql_grant_script(
            identity_account_grants_path[0], capability=identity_account_grants_path[1]
        )
        identity_recovery_grant_sql = read_psql_grant_script(
            identity_recovery_grants_path[0],
            capability=identity_recovery_grants_path[1],
        )
        lan_production_grant_sql = read_psql_grant_script(lan_production_grants_path)
        identity_consumer_grant_sql = read_psql_grant_script(
            identity_consumer_grants_path
        )

        from sqlalchemy.engine import URL, make_url

        from app.config import settings
        from native_storage_identity import (
            NATIVE_CONNECTION_BASE_PREFIX,
            _native_connection_storage_policy,
            _native_source_storage_policy,
        )
        from worktree_env import load_identity, read_state, state_path, topology
        from worktree_resources import (
            LocalResources,
            docker,
            migration_graph,
            unused_loopback_port,
        )
        from worktree_runtime import parse_arguments, run_processes

        if settings.environment != "development":
            raise RuntimeError("module-data acceptance requires development settings")
        if native_lifecycle_profile:
            from urllib.parse import urlsplit

            from native_storage_acceptance import (
                native_profile_environment,
                provision_native_provider,
                qualify_native_provider,
                start_native_provider,
            )
            from storage_s3_contract import sha256
            from verify_storage_candidates import Owner, private_write

            phase = "native-provider-qualification"
            evidence["cleanup"]["native_contract_owner_manifest"] = str(
                output.with_suffix(".native-contract-owner.json")
            )
            evidence["cleanup"]["native_contract_report"] = str(
                output.with_suffix(".native-contract.json")
            )
            qualify_native_provider(output, evidence["native_provider"])
            phase = "native-provider-start"
            native_owner_path = output.with_suffix(".native-provider-owner.json")
            evidence["cleanup"]["native_provider_owner_manifest"] = str(
                native_owner_path
            )
            native_provider_owner = Owner(native_owner_path, create=True)
            native_provider_state = start_native_provider(
                native_provider_owner, evidence["native_provider"]
            )
        owner_password = f"owner-{secrets.token_hex(16)}"
        runtime_password = f"runtime-{secrets.token_hex(16)}"
        identity_api_consumer_password = secrets.token_hex(24)
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
        preference_password = f"notification-preference-{secrets.token_hex(16)}"
        notification_append_password = f"notification-append-{secrets.token_hex(16)}"
        notification_inbox_password = f"notification-inbox-{secrets.token_hex(16)}"
        notification_delivery_read_password = (
            f"notification-delivery-read-{secrets.token_hex(16)}"
        )
        audit_append_password = f"audit-append-{secrets.token_hex(16)}"
        audit_query_password = f"audit-query-{secrets.token_hex(16)}"
        notification_read_password = f"notification-read-{secrets.token_hex(16)}"
        audit_read_password = f"audit-read-{secrets.token_hex(16)}"
        audit_daily_refresh_password = f"audit-daily-refresh-{secrets.token_hex(16)}"
        audit_maintenance_password = f"audit-maintenance-{secrets.token_hex(16)}"
        jobs_notification_scope_password = f"jobs-scope-{secrets.token_hex(16)}"
        jobs_list_query_password = f"jobs-list-{secrets.token_hex(16)}"
        jobs_lifecycle_password = f"jobs-lifecycle-{secrets.token_hex(16)}"
        jobs_dispatch_relay_password = secrets.token_hex(24)
        jobs_publication_password = secrets.token_hex(24)
        export_execution_password = secrets.token_hex(24)
        inference_registry_ingress_password = secrets.token_hex(24)
        inference_routing_password = secrets.token_hex(24)
        inference_owner_passwords = {
            label: secrets.token_hex(24) for label in INFERENCE_OWNER_PROFILES
        }
        video_execution_password = secrets.token_hex(24)
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
            f"127.0.0.1:{unused_loopback_port()}:5432",
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
        evidence["phase"] = "postgres-started"
        _write_evidence(output, evidence)
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
            identity_api_consumer_password,
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
                    "connector_host_allowlist": (
                        [
                            *settings.connector_host_allowlist,
                            urlsplit(native_provider_state["endpoint"]).hostname,
                        ]
                        if native_provider_state is not None
                        else settings.connector_host_allowlist
                    ),
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
        manifest = read_state(state_path(resource_root, "test", "resources.json"))
        birth_oid = manifest.get("postgres", {}).get("database_oid")
        if manifest != backend.manifest or type(birth_oid) is not int or birth_oid <= 0:
            raise RuntimeError("Owned test database birth identity is unproven")

        async def observe_owned_database():
            import asyncpg

            connection = await asyncpg.connect(
                backend.url.set(drivername="postgresql").render_as_string(
                    hide_password=False
                ),
                timeout=5,
            )
            try:
                return dict(
                    await connection.fetchrow(
                        "SELECT d.oid::bigint AS oid,d.datname AS name,"
                        "pg_catalog.shobj_description(d.oid,'pg_database') AS marker,"
                        "current_user AS current_user,session_user AS session_user "
                        "FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
                    )
                )
            finally:
                await connection.close()

        database_identity = asyncio.run(observe_owned_database())
        if not (
            database_identity.get("oid") == birth_oid
            and database_identity.get("name") == database == backend.url.database
            and database_identity.get("marker") == resources["owner"]
            and database_identity.get("current_user") == backend.url.username == "owner"
            and database_identity.get("session_user") == "owner"
        ):
            raise RuntimeError("Owned test database identity is unproven")
        database_oid = database_identity["oid"]
        evidence["database_oid"] = database_oid
        evidence["database"] = database
        _, heads = migration_graph(ROOT)
        if len(heads) != 1:
            raise RuntimeError("module-data acceptance requires one migration head")
        evidence["migrations"]["head_before_frozen_install"] = heads[0]
        relation_sql_hashes = _read_relation_sql_input_sha256()
        evidence["migrations"]["read_relation_sql_input_sha256"] = relation_sql_hashes
        read_relations_before = _installed_read_relation_evidence(
            container_id,
            container_name,
            container_identity,
            database=database,
            owner_password=owner_password,
            expected_owner="owner",
        )
        evidence["migrations"]["read_relations_before_frozen_install"] = (
            read_relations_before
        )
        installed_function_bodies = _installed_function_bodies(
            container_id,
            container_name,
            container_identity,
            database=database,
            owner_password=owner_password,
        )
        evidence["migrations"]["installed_function_body_sha256"] = (
            installed_function_bodies
        )
        phase = "prepare-fresh-main-base"
        frozen_database = f"{database.removesuffix('_test')}_frozen_test"
        frozen_identity = json.loads(
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database="postgres",
                sql=(
                    'CREATE DATABASE :"frozen_database" OWNER owner;\n'
                    "COMMENT ON DATABASE :\"frozen_database\" IS :'marker';\n"
                    "SELECT json_build_object('name', datname, 'oid', oid::bigint, "
                    "'marker', shobj_description(oid, 'pg_database')) "
                    "FROM pg_database WHERE datname = :'frozen_database';"
                ),
                variables={
                    "frozen_database": frozen_database,
                    "marker": f"module-data-frozen:{container_identity}",
                },
            )
        )
        evidence["migrations"]["frozen_database"] = frozen_database
        evidence["migrations"]["frozen_database_identity"] = frozen_identity
        base_revision = evidence["migrations"]["base_revision"]
        _run_alembic(backend, "upgrade", base_revision, database=frozen_database)
        observed_base = _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=frozen_database,
            sql="SELECT version_num FROM alembic_version;",
        ).strip()
        if observed_base != base_revision:
            raise RuntimeError("disposable database did not reach the main schema base")
        evidence["migrations"]["fresh_main_base_prepared"] = True
        phase = "native-frozen-sql-roundtrip"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        read_relations_frozen = _install_and_rollback_frozen_sql(
            container_id,
            container_name,
            container_identity,
            frozen_database,
            owner_password,
            artifacts,
            migration_role="owner",
        )
        _assert_read_relation_structure_matches(
            read_relations_before,
            read_relations_frozen,
            phase="frozen SQL reinstall/rollback",
        )
        evidence["migrations"]["read_relations_frozen_sql_transaction"] = (
            read_relations_frozen
        )
        evidence["migrations"]["frozen_sql_installed_and_rolled_back"] = True
        phase = "insights-frozen-sql-roundtrip"
        _run_alembic(backend, "upgrade", "bo0486", database=frozen_database)
        evidence["migrations"]["insights_frozen_sql"] = (
            _replay_insights_storage_contract(
                container_id,
                container_name,
                container_identity,
                database=frozen_database,
                owner_password=owner_password,
            )
        )
        phase = "upgrade-to-head"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _run_alembic(backend, "upgrade", "head", database=frozen_database)
        observed_head = _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=frozen_database,
            sql="SELECT version_num FROM alembic_version;",
        ).strip()
        if observed_head != heads[0]:
            raise RuntimeError("fresh frozen-SQL database did not reach migration head")
        evidence["migrations"]["upgraded_to_head"] = True
        evidence["migrations"]["head_after_upgrade"] = heads[0]
        read_relations_after = _installed_read_relation_evidence(
            container_id,
            container_name,
            container_identity,
            database=frozen_database,
            owner_password=owner_password,
            expected_owner="owner",
        )
        _assert_read_relation_structure_matches(
            read_relations_before,
            read_relations_after,
            phase="migration upgrade",
        )
        evidence["migrations"]["read_relations_after_upgrade"] = read_relations_after
        upgraded_bodies = _installed_function_bodies(
            container_id,
            container_name,
            container_identity,
            database=frozen_database,
            owner_password=owner_password,
        )
        if upgraded_bodies != installed_function_bodies:
            raise RuntimeError(
                "installed SQL function bodies differ after fresh-base upgrade"
            )
        if not collect_only:
            phase = "preflight-migration-owned-assets-executors"
            fresh_password = secrets.token_hex(32)
            fresh_roles = (
                "fresh_lan_asset_cleanup",
                "fresh_lan_asset_video",
                "fresh_lan_asset_image",
            )
            fresh_owner = {
                "username": "owner",
                "password": owner_password,
                "database": frozen_database,
            }
            _psql(
                container_id,
                container_name,
                container_identity,
                **fresh_owner,
                sql="\n".join(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{fresh_password}' "
                    "NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS;"
                    for role in fresh_roles
                ),
            )
            grant_scope = {
                "expected_database": frozen_database,
                "migration_role": "owner",
            }
            for worker_grant_sql, parameters in (
                (
                    lan_production_grant_sql,
                    {
                        "application_role": "legacy_runtime",
                        "asset_cleanup_role": fresh_roles[0],
                    },
                ),
                (
                    media_grant_sql,
                    {
                        "assets_owner": "owner",
                        "asset_media_worker_role": fresh_roles[1],
                    },
                ),
                (
                    image_grant_sql,
                    {
                        "assets_owner": "owner",
                        "asset_image_worker_role": fresh_roles[2],
                    },
                ),
            ):
                _psql(
                    container_id,
                    container_name,
                    container_identity,
                    **fresh_owner,
                    sql=worker_grant_sql,
                    variables={**grant_scope, **parameters},
                )
            fresh_app_probe = _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=frozen_database,
                sql="""
                    SELECT current_user = 'legacy_runtime'
                      AND has_function_privilege(
                        'yoao_assets_v1.get_catalog_dataset(uuid,uuid)', 'EXECUTE')
                      AND has_function_privilege(
                        'yoao_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb)', 'EXECUTE')
                      AND has_function_privilege(
                        'yoao_assets_v1.read_media_summaries(uuid[])', 'EXECUTE')
                      AND has_function_privilege(
                        'yoao_assets_v1.read_image_usage()', 'EXECUTE')
                      AND NOT EXISTS (
                        SELECT 1 FROM unnest(ARRAY[
                          'yoao_assets_v1.begin_import_original_verification(jsonb)',
                          'yoao_assets_v1.capture_storage_connection(uuid,uuid,text)',
                          'yoao_assets_v1.capture_native_pyramid_read_io(uuid,uuid,jsonb)',
                          'yoao_assets_v1.claim_video_media(uuid,uuid,integer,text[])',
                          'yoao_assets_v1.claim_image_media(uuid,uuid,integer,text[])'
                        ]) signature WHERE has_function_privilege(signature, 'EXECUTE')
                      )
                      AND NOT EXISTS (
                        SELECT FROM unnest(ARRAY[
                          'yoao_assets_v1.import_original_verifications',
                          'public.async_jobs',
                          'public.asset_media_jobs',
                          'public.asset_image_jobs',
                          'public.video_derivative_outputs'
                        ]) relation
                        WHERE has_table_privilege(relation, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                           OR has_any_column_privilege(relation, 'SELECT,INSERT,UPDATE,REFERENCES')
                      );
                """,
            ).strip()
            if fresh_app_probe != "t":
                raise RuntimeError(
                    "Fresh LAN Assets control LOGIN has an incorrect capability set"
                )
            for role, function in (
                (fresh_roles[1], "read_video_media_usage()"),
                (
                    fresh_roles[2],
                    "claim_image_media(gen_random_uuid(),NULL,30,ARRAY['thumbnail'])",
                ),
            ):
                reply_ok = _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username=role,
                    password=fresh_password,
                    database=frozen_database,
                    sql=(
                        "SELECT session_user = :'role' AND current_user = session_user "
                        f"AND (yoao_assets_v1.{function}->>'ok')::boolean;"
                    ),
                    variables={"role": role},
                ).strip()
                if reply_ok != "t":
                    raise RuntimeError(
                        "Fresh LAN Assets executor could not call its worker authority"
                    )
            evidence["assets_migration_owned_execution"] = {
                "ordinary_control": {"role": "legacy_runtime", "passed": True},
                "celery_video_login": {"role": fresh_roles[1], "passed": True},
                "celery_image_login": {"role": fresh_roles[2], "passed": True},
            }
        phase = "retire-frozen-sql-database"
        observed_frozen_identity = json.loads(
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database="postgres",
                sql=(
                    "SELECT json_build_object('name', datname, 'oid', oid::bigint, "
                    "'marker', shobj_description(oid, 'pg_database')) "
                    "FROM pg_database WHERE datname = :'frozen_database';"
                ),
                variables={"frozen_database": frozen_database},
            )
        )
        if observed_frozen_identity != frozen_identity:
            raise RuntimeError("Frozen schema database identity changed; retain it")
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database="postgres",
            sql='DROP DATABASE :"frozen_database";',
            variables={"frozen_database": frozen_database},
        )
        evidence["migrations"]["frozen_database_retired"] = True
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        asyncio.run(backend.grant_runtime())
        evidence["migrations"]["runtime_grants_reapplied"] = True
        append_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_append_client",
            password=append_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        cleanup_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_cleanup_client",
            password=cleanup_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        catalog_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_catalog_client",
            password=catalog_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        source_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_source_client",
            password=source_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        import_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_import_client",
            password=import_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        operator_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_operator_client",
            password=operator_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        media_control_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_media_control_client",
            password=media_control_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        media_worker_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_media_worker_client",
            password=media_worker_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        image_worker_url = URL.create(
            "postgresql+asyncpg",
            username="yoao_asset_image_worker_client",
            password=image_worker_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        notification_preferences_url = URL.create(
            "postgresql",
            username=NOTIFICATION_PREFERENCES_ROLE,
            password=preference_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        notification_append_url = URL.create(
            "postgresql",
            username=NOTIFICATION_APPEND_ROLE,
            password=notification_append_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        notification_inbox_url = URL.create(
            "postgresql",
            username=NOTIFICATION_INBOX_ROLE,
            password=notification_inbox_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        notification_delivery_read_url = URL.create(
            "postgresql",
            username=NOTIFICATION_DELIVERY_READ_ROLE,
            password=notification_delivery_read_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        jobs_list_query_url = URL.create(
            "postgresql",
            username=JOBS_LIST_QUERY_ROLE,
            password=jobs_list_query_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        jobs_lifecycle_url = URL.create(
            "postgresql",
            username=JOBS_LIFECYCLE_ROLE,
            password=jobs_lifecycle_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        jobs_dispatch_relay_url = URL.create(
            "postgresql",
            username=JOBS_DISPATCH_RELAY_ROLE,
            password=jobs_dispatch_relay_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        jobs_publication_url = URL.create(
            "postgresql+asyncpg",
            username=JOBS_PUBLICATION_ROLE,
            password=jobs_publication_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        export_execution_url = URL.create(
            "postgresql+asyncpg",
            username=EXPORT_EXECUTION_ROLE,
            password=export_execution_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        audit_append_url = URL.create(
            "postgresql",
            username=AUDIT_APPEND_ROLE,
            password=audit_append_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        audit_query_url = URL.create(
            "postgresql",
            username=AUDIT_QUERY_ROLE,
            password=audit_query_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        notification_read_url = URL.create(
            "postgresql",
            username=NOTIFICATION_READ_ROLE,
            password=notification_read_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        audit_read_url = URL.create(
            "postgresql",
            username=AUDIT_READ_ROLE,
            password=audit_read_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        audit_maintenance_url = URL.create(
            "postgresql",
            username=AUDIT_MAINTENANCE_ROLE,
            password=audit_maintenance_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        audit_daily_refresh_url = URL.create(
            "postgresql",
            username=AUDIT_DAILY_REFRESH_ROLE,
            password=audit_daily_refresh_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        jobs_notification_scope_url = URL.create(
            "postgresql",
            username=JOBS_NOTIFICATION_SCOPE_ROLE,
            password=jobs_notification_scope_password,
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
            preference_password,
            notification_append_password,
            notification_inbox_password,
            notification_delivery_read_password,
            audit_append_password,
            audit_query_password,
            notification_read_password,
            audit_read_password,
            audit_daily_refresh_password,
            audit_maintenance_password,
            jobs_notification_scope_password,
            jobs_list_query_password,
            jobs_lifecycle_password,
            jobs_dispatch_relay_password,
            jobs_publication_password,
            export_execution_password,
        )
        phase = "create-video-execution-client-role"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                f"CREATE ROLE {VIDEO_EXECUTION_ROLE} LOGIN "
                f"PASSWORD '{video_execution_password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "NOREPLICATION NOBYPASSRLS;"
            ),
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
        phase = "create-inference-registry-ingress-client"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "CREATE ROLE yoao_inference_registry_ingress_client LOGIN "
                f"PASSWORD '{inference_registry_ingress_password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "NOREPLICATION NOBYPASSRLS;\n"
            ),
        )
        phase = "create-inference-routing-client"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "CREATE ROLE yoao_inference_routing_client LOGIN "
                f"PASSWORD '{inference_routing_password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "NOREPLICATION NOBYPASSRLS;\n"
            ),
        )
        phase = "create-inference-owner-clients"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql="\n".join(
                f"CREATE ROLE {role} LOGIN PASSWORD '{inference_owner_passwords[label]}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;"
                for label, (_, role) in INFERENCE_OWNER_PROFILES.items()
            ),
        )
        phase = "install-module-grants"
        module_grant_variables = {
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
            "workflow_role": "yoao_append_client",
            "asset_cleanup_role": "yoao_asset_cleanup_client",
            "mask_gc_role": "yoao_labeling_mask_gc_client",
            "inference_registry_ingress_role": INFERENCE_REGISTRY_INGRESS_ROLE,
            "inference_routing_role": INFERENCE_ROUTING_ROLE,
        }
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=grant_sql,
            variables=module_grant_variables,
        )
        phase = "verify-inference-project-grant-reapplication"
        evidence["inference_project_grant_reapplication"] = {}
        _verify_inference_project_grant_reapplication(
            container_id,
            container_name,
            container_identity,
            database=database,
            owner_password=owner_password,
            grant_sql=grant_sql,
            variables=module_grant_variables,
            evidence=evidence["inference_project_grant_reapplication"],
        )
        read_relations_after_owner_grants = _installed_read_relation_evidence(
            container_id,
            container_name,
            container_identity,
            database=database,
            owner_password=owner_password,
            expected_owner={
                relation_name: spec["owner"]
                for relation_name, spec in READ_RELATION_SPECS.items()
            },
        )
        _assert_read_relation_structure_matches(
            read_relations_after,
            read_relations_after_owner_grants,
            phase="module owner transfer",
        )
        evidence["migrations"]["read_relations_after_owner_grants"] = (
            read_relations_after_owner_grants
        )

        def apply_notification_preferences_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=notification_preferences_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "notifications_owner": "notifications_code",
                    "api_role": "legacy_runtime",
                    "preference_role": NOTIFICATION_PREFERENCES_ROLE,
                },
            )

        def apply_notification_append_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=notification_append_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "notifications_owner": "notifications_code",
                    "api_role": "legacy_runtime",
                    "append_role": NOTIFICATION_APPEND_ROLE,
                },
            )

        def apply_notification_inbox_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=notification_inbox_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "notifications_owner": "notifications_code",
                    "api_role": "legacy_runtime",
                    "query_client": NOTIFICATION_INBOX_ROLE,
                },
            )

        def apply_notification_delivery_read_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=notification_delivery_read_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "notifications_owner": "notifications_code",
                    "api_role": "legacy_runtime",
                    "query_client": NOTIFICATION_DELIVERY_READ_ROLE,
                },
            )

        def apply_jobs_list_query_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=read_psql_grant_script(
                    jobs_list_query_grants_path[0],
                    capability=jobs_list_query_grants_path[1],
                ),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "jobs_owner": "jobs_code",
                    "api_role": "legacy_runtime",
                    "list_client": JOBS_LIST_QUERY_ROLE,
                },
            )

        def apply_jobs_lifecycle_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=read_psql_grant_script(
                    jobs_lifecycle_grants_path[0],
                    capability=jobs_lifecycle_grants_path[1],
                ),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "jobs_owner": "jobs_code",
                    "api_role": "legacy_runtime",
                    "lifecycle_client": JOBS_LIFECYCLE_ROLE,
                },
            )

        def apply_jobs_dispatch_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=read_psql_grant_script(
                    jobs_dispatch_grants_path[0],
                    capability=jobs_dispatch_grants_path[1],
                ),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "jobs_owner": "jobs_code",
                    "api_role": "legacy_runtime",
                    "dispatch_relay_role": JOBS_DISPATCH_RELAY_ROLE,
                },
            )

        def apply_execution_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=read_psql_grant_script(
                    execution_grants_path[0], capability=execution_grants_path[1]
                ),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "jobs_owner": "jobs_code",
                    "export_owner": "export_code",
                    "jobs_publication_role": JOBS_PUBLICATION_ROLE,
                    "export_execution_role": EXPORT_EXECUTION_ROLE,
                },
            )

        def apply_identity_actor_status_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=identity_actor_status_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "identity_owner": "identity_code",
                    "status_role": IDENTITY_CLIENT_ROLES["ACTOR_STATUS"],
                },
            )

        def apply_notification_read_grants(api_role: str) -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=notification_read_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "notifications_owner": "notifications_code",
                    "api_role": api_role,
                },
            )

        def apply_audit_append_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=audit_append_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "audit_owner": "audit_code",
                    "api_role": "legacy_runtime",
                    "append_client": AUDIT_APPEND_ROLE,
                },
            )

        def apply_audit_query_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=audit_query_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "audit_owner": "audit_code",
                    "identity_owner": "identity_code",
                    "api_role": "legacy_runtime",
                    "query_client": AUDIT_QUERY_ROLE,
                },
            )

        def apply_audit_read_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=audit_read_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "audit_owner": "audit_code",
                    "api_role": "legacy_runtime",
                    "read_client": AUDIT_READ_ROLE,
                },
            )

        def apply_audit_maintenance_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=audit_maintenance_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "audit_owner": "audit_code",
                    "maintenance_role": AUDIT_MAINTENANCE_ROLE,
                },
            )

        def apply_jobs_scope_grants() -> None:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=jobs_scope_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "jobs_owner": "jobs_code",
                    "api_role": "legacy_runtime",
                    "read_client": JOBS_NOTIFICATION_SCOPE_ROLE,
                },
            )

        phase = "install-notification-preferences-grants"
        apply_notification_preferences_grants()
        evidence["notification_preferences"]["api_function_grant"] = True
        evidence["notification_preferences"]["function_only_role_grant"] = True
        phase = "install-notification-append-grants"
        apply_notification_append_grants()
        evidence["notification_append"]["api_function_grant"] = True
        evidence["notification_append"]["api_insert_revoked"] = True
        evidence["notification_append"]["function_only_role_grant"] = True
        evidence["notification_terminal_append"]["api_function_grant"] = True
        evidence["notification_terminal_append"]["function_only_role_grant"] = True
        evidence["notification_read_state"]["api_function_grant"] = True
        evidence["notification_read_state"]["api_update_revoked"] = True
        evidence["notification_read_state"]["function_only_role_grant"] = True
        evidence["notification_delete"]["api_function_grant"] = True
        evidence["notification_delete"]["api_delete_revoked"] = True
        evidence["notification_delete"]["function_only_role_grant"] = True
        phase = "install-jobs-list-query-grants"
        apply_jobs_list_query_grants()
        apply_jobs_lifecycle_grants()
        phase = "install-jobs-dispatch-grants"
        apply_jobs_dispatch_grants()
        phase = "install-execution-grants"
        apply_execution_grants()

        async def verify_execution_and_fixture_roles() -> None:
            import asyncpg
            from fixture_media_control import (
                _ASSETS,
                _JOBS,
                _RETIRED_PRODUCER_FUNCTIONS,
            )
            from fixture_native_read_control import (
                _READER_FUNCTIONS,
                _RESOLVER_FUNCTIONS,
            )
            from yoao_owner_data.postgres import verify_function_only_role

            profiles = (
                (
                    jobs_publication_url,
                    JOBS_PUBLICATION_ROLE,
                    "yoao_jobs_v1",
                    {
                        (
                            "begin_dispatch_publication",
                            "uuid, uuid, text, bigint, uuid",
                        ),
                        (
                            "finish_dispatch_publication",
                            "uuid, uuid, text, bigint, uuid",
                        ),
                        (
                            "refuse_dispatch_publication",
                            "uuid, uuid, text, bigint, uuid",
                        ),
                        ("retry_publication_intent", "uuid, text, text, jsonb, jsonb"),
                        (
                            "begin_retry_publication",
                            "uuid, uuid, text, text, jsonb, jsonb, integer, uuid",
                        ),
                        ("finish_retry_publication", "uuid, uuid, text, integer, uuid"),
                        ("begin_fixture_media_publication", "uuid, uuid"),
                        ("finish_fixture_media_publication", "uuid, uuid, text"),
                    },
                ),
                (
                    export_execution_url,
                    EXPORT_EXECUTION_ROLE,
                    "yoao_export_v1",
                    {
                        ("begin_execution", "uuid, uuid, text, uuid, integer"),
                        ("enter_execution_io", "uuid, uuid"),
                        ("quiesce_execution_io", "uuid, uuid"),
                        ("finish_execution", "uuid, uuid"),
                    },
                ),
            )
            for url, role, schema, signatures in profiles:
                connection = await asyncpg.connect(
                    url.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=5
                )
                try:
                    await verify_function_only_role(
                        connection, schema=schema, functions=signatures, role=role
                    )
                finally:
                    await connection.close()

            connection = await asyncpg.connect(
                host="127.0.0.1",
                port=port,
                user="owner",
                password=owner_password,
                database=database,
                timeout=5,
            )
            try:
                private_signatures = (
                    _JOBS
                    + _ASSETS
                    + _RETIRED_PRODUCER_FUNCTIONS
                    + _READER_FUNCTIONS
                    + _RESOLVER_FUNCTIONS
                    + (
                        "yoao_jobs_v1.delete_fixture_media_backfills(uuid,uuid)",
                        "yoao_assets_v1.record_fixture_external_pyramid_reuse(text,uuid,uuid,uuid,integer)",
                        "yoao_assets_v1.fixture_external_image_attempt_sources_proven(uuid,uuid,uuid,uuid,bigint,uuid)",
                    )
                )
                if await connection.fetchval(
                    """SELECT EXISTS (
                        SELECT FROM pg_catalog.unnest($1::text[]) AS signature
                        WHERE pg_catalog.has_function_privilege(
                            'legacy_runtime',pg_catalog.to_regprocedure(signature),'EXECUTE')
                    )""",
                    list(private_signatures),
                ):
                    raise RuntimeError(
                        "ordinary API can execute private fixture capabilities"
                    )
                transaction = connection.transaction()
                await transaction.start()
                try:
                    for role, schemas, signatures in (
                        (
                            "fixture_media_acl_probe",
                            ("yoao_jobs_v1", "yoao_assets_v1"),
                            _JOBS + _ASSETS,
                        ),
                        (
                            "fixture_reader_acl_probe",
                            ("yoao_assets_v1",),
                            _READER_FUNCTIONS,
                        ),
                        (
                            "fixture_resolver_acl_probe",
                            ("yoao_assets_v1",),
                            _RESOLVER_FUNCTIONS,
                        ),
                    ):
                        await connection.execute(
                            f"CREATE ROLE {role} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                        )
                        await connection.execute(
                            f"GRANT USAGE ON SCHEMA {','.join(schemas)} TO {role}"
                        )
                        await connection.execute(
                            f"GRANT EXECUTE ON FUNCTION {','.join(signatures)} TO {role}"
                        )
                        expected = await connection.fetch(
                            """SELECT p.oid, n.nspname, pg_catalog.pg_get_userbyid(p.proowner) AS owner
                            FROM pg_catalog.unnest($1::text[]) AS signature
                            LEFT JOIN pg_catalog.pg_proc AS p
                              ON p.oid=pg_catalog.to_regprocedure(signature)
                            LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace""",
                            list(signatures),
                        )
                        if any(
                            row["owner"]
                            != {
                                "yoao_assets_v1": "assets_code",
                                "yoao_jobs_v1": "jobs_code",
                            }.get(row["nspname"])
                            for row in expected
                        ):
                            raise RuntimeError(
                                f"fixture capability owner differs: {role}"
                            )
                        expected_oids = {row["oid"] for row in expected}
                        actual = await connection.fetch(
                            """SELECT p.oid FROM pg_catalog.pg_proc AS p
                            JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
                            WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                              AND n.nspname NOT LIKE 'pg_toast%'
                              AND n.nspname NOT LIKE 'pg_temp_%'
                              AND (n.nspname LIKE 'aap\\_%' ESCAPE '\\'
                                   OR n.nspname LIKE 'yoao\\_%' ESCAPE '\\' OR p.prosecdef)
                              AND p.prorettype NOT IN (
                                  'pg_catalog.trigger'::pg_catalog.regtype,
                                  'pg_catalog.event_trigger'::pg_catalog.regtype)
                              AND NOT EXISTS (
                                  SELECT FROM pg_catalog.pg_depend AS d
                                  WHERE d.classid='pg_catalog.pg_proc'::pg_catalog.regclass
                                    AND d.objid=p.oid
                                    AND d.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass
                                    AND d.deptype='e')
                              AND pg_catalog.has_function_privilege($1,p.oid,'EXECUTE')""",
                            role,
                        )
                        if None in expected_oids or expected_oids != {
                            row["oid"] for row in actual
                        }:
                            raise RuntimeError(
                                f"fixture capability profile differs: {role}"
                            )
                        if await connection.fetchval(
                            """SELECT EXISTS (
                                SELECT FROM pg_catalog.pg_class AS c
                                JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
                                WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                                  AND n.nspname NOT LIKE 'pg_toast%'
                                  AND n.nspname NOT LIKE 'pg_temp_%'
                                  AND c.relkind IN ('r','p','v','m','f')
                                  AND (pg_catalog.has_table_privilege($1,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                                    OR pg_catalog.has_any_column_privilege($1,c.oid,'SELECT,INSERT,UPDATE,REFERENCES'))
                            )""",
                            role,
                        ):
                            raise RuntimeError(
                                f"fixture role has relation privileges: {role}"
                            )
                finally:
                    await transaction.rollback()
            finally:
                await connection.close()

        asyncio.run(verify_execution_and_fixture_roles())
        evidence["jobs_list_query"]["function_only_grants"] = True
        phase = "install-notification-delivery-read-grants"
        apply_notification_delivery_read_grants()
        evidence["notification_delivery_read"]["api_function_grant"] = True
        evidence["notification_read_relations"]["api_owner_function_grant"] = True

        async def verify_delivery_read_login() -> None:
            import asyncpg

            from yoao_owner_data.postgres import verify_function_only_role

            connection = await asyncpg.connect(
                notification_delivery_read_url, timeout=5
            )
            try:
                await verify_function_only_role(
                    connection,
                    schema="yoao_notifications_v1",
                    functions={
                        (
                            signature.split(".", 1)[1].split("(", 1)[0],
                            signature.split("(", 1)[1][:-1].replace(",", ", "),
                        )
                        for signature in NOTIFICATION_DELIVERY_READ_FUNCTIONS
                    },
                    role=NOTIFICATION_DELIVERY_READ_ROLE,
                )
            finally:
                await connection.close()

        asyncio.run(verify_delivery_read_login())
        evidence["notification_delivery_read"]["function_only_login"] = True
        phase = "install-notification-inbox-grants"
        apply_notification_inbox_grants()
        evidence["notification_inbox"]["api_function_grant"] = True
        evidence["notification_inbox"]["function_only_role_grant"] = True

        async def verify_inbox_login() -> None:
            import asyncpg

            from yoao_owner_data.postgres import verify_function_only_role

            connection = await asyncpg.connect(notification_inbox_url, timeout=5)
            try:
                await verify_function_only_role(
                    connection,
                    schema="yoao_notifications_v1",
                    functions={
                        (
                            signature.split(".", 1)[1].split("(", 1)[0],
                            signature.split("(", 1)[1][:-1].replace(",", ", "),
                        )
                        for signature in NOTIFICATION_INBOX_FUNCTIONS
                    },
                    role=NOTIFICATION_INBOX_ROLE,
                )
            finally:
                await connection.close()

        asyncio.run(verify_inbox_login())
        evidence["notification_inbox"]["function_only_login"] = True
        phase = "verify-notification-inbox-acl-guards"
        evidence["notification_inbox"]["acl_guard_probes"] = asyncio.run(
            _verify_inbox_grant_guards(
                URL.create(
                    "postgresql",
                    username="owner",
                    password=owner_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                notification_inbox_grant_sql,
            )
        )
        phase = "install-audit-append-grants"
        apply_audit_append_grants()
        evidence["audit_append"]["api_function_grant"] = True
        evidence["audit_append"]["api_insert_revoked"] = True
        evidence["audit_append"]["function_only_role_grant"] = True
        phase = "install-audit-query-grants"
        apply_audit_query_grants()
        evidence["audit_query"]["api_function_grant"] = True
        evidence["audit_query"]["function_only_role_grant"] = True
        phase = "install-notification-read-relation-client-grants"
        apply_notification_read_grants(NOTIFICATION_READ_ROLE)
        evidence["notification_read_relations"]["read_client_grant"] = True
        phase = "install-audit-read-relation-grants"
        apply_audit_read_grants()
        evidence["audit_read_relations"]["api_relation_grant"] = True
        evidence["audit_read_relations"]["read_client_grant"] = True
        phase = "install-audit-maintenance-grants"
        apply_audit_maintenance_grants()
        evidence["audit_maintenance"]["function_only_role_grant"] = True
        phase = "verify-audit-maintenance-boundary"
        evidence["audit_maintenance"].update(
            _verify_audit_maintenance_boundary(
                container_id,
                container_name,
                container_identity,
                database=database,
                owner_password=owner_password,
                runtime_password=runtime_password,
                maintenance_password=audit_maintenance_password,
                maintenance_url=audit_maintenance_url,
            )
        )
        phase = "install-jobs-notification-scope-grants"
        apply_jobs_scope_grants()
        evidence["jobs_notification_scope"]["api_relation_grant"] = True
        evidence["jobs_notification_scope"]["read_client_grant"] = True
        phase = "verify-audit-daily-projection-native"
        evidence["audit_daily_projection"] = {
            "status": "passed",
            **_verify_audit_daily_projection_boundary(
                container_id,
                container_name,
                container_identity,
                database=database,
                owner_password=owner_password,
                runtime_password=runtime_password,
                read_password=audit_read_password,
                refresh_password=audit_daily_refresh_password,
                refresh_url=audit_daily_refresh_url,
            ),
        }
        read_role_contract = _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "SELECT pg_catalog.count(*)=6 AND pg_catalog.bool_and("
                "role.rolcanlogin AND NOT role.rolinherit "
                "AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole "
                "OR role.rolreplication OR role.rolbypassrls) "
                "AND pg_catalog.has_database_privilege("
                "role.oid, pg_catalog.current_database(), 'CONNECT') "
                "AND NOT pg_catalog.has_database_privilege("
                "role.oid, pg_catalog.current_database(), 'CREATE') "
                "AND NOT pg_catalog.has_database_privilege("
                "role.oid, pg_catalog.current_database(), 'TEMP') "
                "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS membership "
                "WHERE membership.member=role.oid OR membership.roleid=role.oid)) "
                "FROM pg_catalog.pg_roles AS role "
                f"WHERE role.rolname IN ('{NOTIFICATION_READ_ROLE}','{AUDIT_READ_ROLE}',"
                f"'{AUDIT_DAILY_REFRESH_ROLE}','{JOBS_NOTIFICATION_SCOPE_ROLE}',"
                f"'{NOTIFICATION_INBOX_ROLE}','{NOTIFICATION_DELIVERY_READ_ROLE}');"
            ),
        )
        if read_role_contract.strip() != "t":
            raise RuntimeError(
                "Read relation clients are not isolated NOINHERIT LOGINS"
            )
        notification_preference_user_id = str(uuid4())
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "INSERT INTO public.users "
                "(id, email, name, password_hash, role, is_active) VALUES ("
                f"'{notification_preference_user_id}'::uuid, "
                f"'notification-preference-{notification_preference_user_id}@test.invalid', "
                "'Preference owner fixture', 'module-data-fixture', 'employee', true);"
            ),
        )
        api_get_request = json.dumps(
            {
                "schema": "yoao-notification-preferences/v2",
                "operation": "get",
                "user_id": notification_preference_user_id,
            },
            separators=(",", ":"),
        )
        api_function_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.preference_command("
                f"'{api_get_request}'::jsonb) ->> 'ok';"
            ),
        )
        if api_function_result.strip() != "true":
            raise RuntimeError(
                "API LOGIN could not call Notifications preference owner"
            )
        try:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                sql="SELECT channels FROM public.notification_preferences LIMIT 1;",
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("API LOGIN retained direct preference table access")
        evidence["notification_preferences"]["api_function_call"] = True
        notification_private_statements = (
            "SELECT * FROM public.notifications LIMIT 0",
            "SELECT * FROM public.notification_preferences LIMIT 0",
            "SELECT * FROM public.notification_delivery_outbox LIMIT 0",
            "INSERT INTO public.notifications "
            "(id,user_id,type,target_type,target_id,payload) "
            "SELECT pg_catalog.gen_random_uuid(), pg_catalog.gen_random_uuid(), "
            "'task.approved', 'task', pg_catalog.gen_random_uuid(), '{}'::jsonb "
            "WHERE false",
            "UPDATE public.notifications SET read_at=pg_catalog.now() WHERE false",
            "DELETE FROM public.notifications WHERE false",
            "INSERT INTO public.notification_preferences (user_id,type,channels) "
            "SELECT pg_catalog.gen_random_uuid(), 'task.approved', '{}'::jsonb WHERE false",
            "UPDATE public.notification_preferences SET channels='{}'::jsonb WHERE false",
            "DELETE FROM public.notification_preferences WHERE false",
            "INSERT INTO public.notification_delivery_outbox "
            "(notification_id,operation_id,recipient_id,notification_type) "
            "SELECT pg_catalog.gen_random_uuid(), pg_catalog.gen_random_uuid(), "
            "pg_catalog.gen_random_uuid(), 'user.deactivation_requested' WHERE false",
            "UPDATE public.notification_delivery_outbox SET attempt=attempt WHERE false",
            "DELETE FROM public.notification_delivery_outbox WHERE false",
        )
        notification_fact_read_statement = (
            "SELECT id FROM yoao_notifications_v1.notification_facts LIMIT 1"
        )
        notification_fact_write_statements = (
            "INSERT INTO yoao_notifications_v1.notification_facts "
            "(id,user_id,type,target_type,target_id,payload) "
            "SELECT pg_catalog.gen_random_uuid(),pg_catalog.gen_random_uuid(),"
            "'task.approved','task',pg_catalog.gen_random_uuid(),'{}'::jsonb WHERE false",
            "UPDATE yoao_notifications_v1.notification_facts "
            "SET read_at=pg_catalog.now() WHERE false",
            "DELETE FROM yoao_notifications_v1.notification_facts WHERE false",
        )
        for statement in notification_private_statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                statement=statement,
            )
        preference_after_private_denials = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.preference_command("
                f"'{api_get_request}'::jsonb) ->> 'ok';"
            ),
        )
        if preference_after_private_denials.strip() != "true":
            raise RuntimeError(
                "Notifications preference owner failed without API table reads"
            )
        evidence["notification_read_relations"]["api_private_reads_revoked"] = True
        notification_append_id = str(uuid4())
        notification_append_request = json.dumps(
            {
                "schema": "aap-notification-append/v1",
                "operation": "append",
                "id": notification_append_id,
                "user_id": notification_preference_user_id,
                "type": "task.approved",
                "target_type": "task",
                "target_id": str(uuid4()),
                "payload": {"source": "module-data-verifier"},
            },
            separators=(",", ":"),
        )
        api_append_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.append_notification("
                f"'{notification_append_request}'::jsonb) ->> 'ok';"
            ),
        )
        if api_append_result.strip() != "true":
            raise RuntimeError("API LOGIN could not call Notifications append owner")
        phase = "verify-notification-terminal-append-api"
        terminal_append_request = json.dumps(
            {
                "schema": "aap-notification-terminal-append/v1",
                "operation": "append_job_terminal",
                "id": str(uuid4()),
                "user_id": notification_preference_user_id,
                "type": "job.completed",
                "target_id": str(uuid4()),
                "payload": {"kind": "batch_predict", "status": "completed"},
            },
            separators=(",", ":"),
        )
        api_terminal_append_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.append_job_terminal_notification("
                f"'{terminal_append_request}'::jsonb) -> 'data' ->> 'created';"
            ),
        )
        if api_terminal_append_result.strip() != "true":
            raise RuntimeError("API LOGIN could not call terminal Notifications owner")
        evidence["notification_terminal_append"]["api_function_call"] = True
        phase = "verify-notification-append-api"
        api_direct_insert_id = str(uuid4())
        try:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                sql=(
                    "INSERT INTO public.notifications "
                    "(id,user_id,type,target_type,target_id,payload) VALUES ("
                    f"'{api_direct_insert_id}'::uuid, "
                    f"'{notification_preference_user_id}'::uuid, "
                    "'task.approved', 'task', "
                    f"'{uuid4()}'::uuid, '{{}}'::jsonb);"
                ),
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("API LOGIN retained direct notification INSERT access")
        evidence["notification_append"]["api_function_call"] = True
        notification_read_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        notification_read_state_request = json.dumps(
            {
                "schema": "aap-notification-read-state/v1",
                "operation": "mark_read",
                "user_id": notification_preference_user_id,
                "notification_id": notification_append_id,
                "read_at": notification_read_at,
            },
            separators=(",", ":"),
        )
        api_read_state_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.read_state_command("
                f"'{notification_read_state_request}'::jsonb) -> 'data' ->> 'updated';"
            ),
        )
        if api_read_state_result.strip() != "true":
            raise RuntimeError(
                "API LOGIN could not call Notifications read-state owner"
            )
        try:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                sql="UPDATE public.notifications SET read_at=now() WHERE false;",
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("API LOGIN retained direct notification UPDATE access")
        evidence["notification_read_state"]["api_function_call"] = True
        notification_delete_request = json.dumps(
            {
                "schema": "aap-notification-delete/v1",
                "operation": "delete_one",
                "user_id": notification_preference_user_id,
                "notification_id": notification_append_id,
            },
            separators=(",", ":"),
        )
        api_delete_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.delete_command("
                f"CAST('{notification_delete_request}' AS pg_catalog.jsonb)) "
                "-> 'data' ->> 'deleted';"
            ),
        )
        if api_delete_result.strip() != "true":
            raise RuntimeError("API LOGIN could not call Notifications delete owner")
        try:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                sql=(
                    "DELETE FROM public.notifications "
                    f"WHERE id='{notification_append_id}'::uuid;"
                ),
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("API LOGIN retained direct notification DELETE access")
        evidence["notification_delete"]["api_function_call"] = True
        read_relation_notification_id = str(uuid4())
        read_relation_append_request = json.dumps(
            {
                "schema": "aap-notification-append/v1",
                "operation": "append",
                "id": read_relation_notification_id,
                "user_id": notification_preference_user_id,
                "type": "task.approved",
                "target_type": "task",
                "target_id": str(uuid4()),
                "payload": {"read_relation": True},
            },
            separators=(",", ":"),
        )
        read_relation_seed_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.append_notification("
                f"'{read_relation_append_request}'::jsonb) "
                "-> 'data' -> 'notification' ->> 'id';"
            ),
        )
        if read_relation_seed_result.strip() != read_relation_notification_id:
            raise RuntimeError("Notifications read-relation seed was not committed")
        api_notification_fact = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT count(*) FROM yoao_notifications_v1.read_notification("
                f"'{read_relation_notification_id}'::uuid) "
                f"WHERE user_id='{notification_preference_user_id}'::uuid "
                f"AND id='{read_relation_notification_id}'::uuid "
                "AND payload = '{\"read_relation\": true}'::jsonb;"
            ),
        )
        if api_notification_fact.strip() != "1":
            raise RuntimeError("API LOGIN could not read Notifications facts")
        for statement in (
            notification_fact_read_statement,
            *notification_fact_write_statements,
        ):
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                statement=statement,
            )
        evidence["notification_read_relations"]["api_owner_function_call"] = True
        evidence["notification_read_relations"]["api_relation_select_denied"] = True
        evidence["notification_delivery_read"]["api_fact_select_denied"] = True
        for statement in (
            "SELECT yoao_notifications_v1.preference_command('{}'::jsonb)",
            "SELECT yoao_notifications_v1.append_notification('{}'::jsonb)",
            "SELECT yoao_notifications_v1.append_job_terminal_notification('{}'::jsonb)",
            "SELECT yoao_notifications_v1.read_state_command('{}'::jsonb)",
            "SELECT yoao_notifications_v1.delete_command('{}'::jsonb)",
            "SELECT yoao_notifications_v1.identity_delivery_policy()",
            "SELECT yoao_notifications_v1.claim_identity_deliveries('{}'::jsonb)",
            "SELECT yoao_notifications_v1.finish_identity_delivery('{}'::jsonb)",
            "SELECT yoao_notifications_v1.create_identity_deactivation_notifications("
            "NULL::uuid,NULL::text,NULL::jsonb,NULL::uuid[])",
        ):
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=NOTIFICATION_READ_ROLE,
                password=notification_read_password,
                database=database,
                statement=statement,
            )
        for statement in notification_private_statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=NOTIFICATION_READ_ROLE,
                password=notification_read_password,
                database=database,
                statement=statement,
            )
        notification_read_function_count = _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "SELECT count(*) FROM pg_catalog.pg_proc AS function "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid=function.pronamespace "
                "WHERE namespace.nspname='yoao_notifications_v1' "
                "AND pg_catalog.has_function_privilege("
                f"'{NOTIFICATION_READ_ROLE}', function.oid, 'EXECUTE');"
            ),
        )
        if notification_read_function_count.strip() != "0":
            raise RuntimeError(
                "Notifications read LOGIN has an owner function capability"
            )
        evidence["notification_read_relations"]["read_client_function_denied"] = True
        audit_append_request = json.dumps(
            {
                "schema": "aap-audit-append/v1",
                "events": [
                    {
                        "actor_id": None,
                        "actor_email": None,
                        "actor_role": None,
                        "action": "audit.append.module-data-verifier",
                        "target_type": None,
                        "target_id": None,
                        "method": "POST",
                        "path": "/audit-module-data-verifier",
                        "status_code": 200,
                        "ip": None,
                        "detail_json": None,
                        "request_id": None,
                    }
                ],
            },
            separators=(",", ":"),
        )
        api_audit_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_audit_v1.append_events("
                f"'{audit_append_request}'::jsonb) -> 'data' -> 'events' -> 0 ->> 'id';"
            ),
        )
        if not api_audit_result.strip().isdigit():
            raise RuntimeError("API LOGIN could not call Audit append owner")
        audit_append_id = api_audit_result.strip()
        try:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                sql="INSERT INTO public.audit_logs (action) VALUES ('audit.direct.denied');",
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("API LOGIN retained direct Audit INSERT access")
        try:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                sql=(
                    "SELECT nextval(pg_catalog.pg_get_serial_sequence("
                    "'public.audit_logs', 'id'));"
                ),
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("API LOGIN retained direct Audit sequence writes")
        evidence["audit_append"]["api_function_call"] = True
        audit_query_request = json.dumps(
            {
                "schema": "aap-audit-query/v1",
                "operation": "list",
                "filters": {
                    "action": None,
                    "target_type": None,
                    "target_id": None,
                    "actor_id": None,
                    "from": None,
                    "to": None,
                    "business_only": False,
                    "detail_key": None,
                    "detail_value": None,
                },
                "page": 1,
                "page_size": 1,
                "after": None,
            },
            separators=(",", ":"),
        )
        api_audit_query_result = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT yoao_audit_v1.query_events("
                f"CAST('{audit_query_request}' AS pg_catalog.jsonb)) "
                "-> 'data' ->> 'operation';"
            ),
        )
        if api_audit_query_result.strip() != "list":
            raise RuntimeError("API LOGIN could not call Audit query owner")
        evidence["audit_query"]["api_function_call"] = True
        api_audit_fact = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT count(*) FROM yoao_audit_v1.audit_events "
                f"WHERE id='{audit_append_id}'::bigint;"
            ),
        )
        if api_audit_fact.strip() != "1":
            raise RuntimeError("API LOGIN could not read Audit event facts")
        audit_fact_write_statements = (
            "INSERT INTO yoao_audit_v1.audit_events (action) "
            "SELECT 'audit.read_relation.denied' WHERE false",
            "UPDATE yoao_audit_v1.audit_events SET action='audit.denied' WHERE false",
            "DELETE FROM yoao_audit_v1.audit_events WHERE false",
        )
        for statement in audit_fact_write_statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username="legacy_runtime",
                password=runtime_password,
                database=database,
                statement=statement,
            )
        evidence["audit_read_relations"]["api_fact_call"] = True
        audit_node_environment = os.environ.copy()
        audit_node_environment["AUDIT_APPEND_DATABASE_URL"] = audit_append_url
        phase = "verify-audit-append-node"
        audit_node_result = subprocess.run(
            [node_bin, str(audit_append_node_provider), "--live"],
            cwd=audit_append_node_provider.parent,
            env=audit_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if audit_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, audit_node_result)
            raise RuntimeError("Node Audit append check failed")
        evidence["audit_append"]["node_contract_check"] = True
        audit_query_node_environment = os.environ.copy()
        audit_query_node_environment["AUDIT_QUERY_DATABASE_URL"] = audit_query_url
        phase = "verify-audit-query-node"
        audit_query_node_result = subprocess.run(
            [node_bin, str(audit_query_node_provider), "--live"],
            cwd=audit_query_node_provider.parent,
            env=audit_query_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if audit_query_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, audit_query_node_result)
            raise RuntimeError("Node Audit query check failed")
        evidence["audit_query"]["node_contract_check"] = True
        audit_read_node_environment = os.environ.copy()
        audit_read_node_environment["AUDIT_READ_DATABASE_URL"] = audit_read_url
        audit_read_node_environment["TZ"] = "Asia/Shanghai"
        phase = "verify-audit-read-relation-node"
        audit_read_node_result = subprocess.run(
            [node_bin, str(audit_read_node_provider), "--live"],
            cwd=audit_read_node_provider.parent,
            env=audit_read_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if audit_read_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, audit_read_node_result)
            raise RuntimeError("Node Audit read-relation check failed")
        evidence["audit_read_relations"]["node_relation_check"] = True
        node_environment = os.environ.copy()
        node_environment["NOTIFICATION_PREFERENCES_DATABASE_URL"] = (
            notification_preferences_url
        )
        node_environment["NOTIFICATION_PREFERENCE_USER_ID"] = (
            notification_preference_user_id
        )
        phase = "verify-notification-preferences-node"
        node_result = subprocess.run(
            [node_bin, str(node_provider)],
            cwd=node_provider.parent,
            env=node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, node_result)
            raise RuntimeError("Node Notifications preference check failed")
        evidence["notification_preferences"]["node_contract_check"] = True
        node_environment["NOTIFICATION_APPEND_DATABASE_URL"] = notification_append_url
        node_environment["NOTIFICATION_APPEND_USER_ID"] = (
            notification_preference_user_id
        )
        phase = "verify-notification-append-node"
        append_node_result = subprocess.run(
            [node_bin, str(append_node_provider)],
            cwd=append_node_provider.parent,
            env=node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if append_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, append_node_result)
            raise RuntimeError("Node Notifications append check failed")
        evidence["notification_append"]["node_contract_check"] = True
        phase = "verify-notification-terminal-append-node"
        terminal_append_node_result = subprocess.run(
            [node_bin, str(terminal_append_node_provider)],
            cwd=terminal_append_node_provider.parent,
            env=node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if terminal_append_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(
                phase, terminal_append_node_result
            )
            raise RuntimeError("Node Notifications terminal append check failed")
        evidence["notification_terminal_append"]["node_contract_check"] = True
        node_environment["NOTIFICATION_READ_STATE_DATABASE_URL"] = (
            notification_append_url
        )
        node_environment["NOTIFICATION_READ_STATE_USER_ID"] = (
            notification_preference_user_id
        )
        phase = "verify-notification-read-state-node"
        read_state_node_result = subprocess.run(
            [node_bin, str(read_state_node_provider)],
            cwd=read_state_node_provider.parent,
            env=node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if read_state_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, read_state_node_result)
            raise RuntimeError("Node Notifications read-state check failed")
        evidence["notification_read_state"]["node_contract_check"] = True
        node_environment["NOTIFICATION_DELETE_DATABASE_URL"] = notification_append_url
        node_environment["NOTIFICATION_DELETE_USER_ID"] = (
            notification_preference_user_id
        )
        phase = "verify-notification-delete-node"
        delete_node_result = subprocess.run(
            [node_bin, str(delete_node_provider)],
            cwd=delete_node_provider.parent,
            env=node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if delete_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, delete_node_result)
            raise RuntimeError("Node Notifications deletion check failed")
        evidence["notification_delete"]["node_contract_check"] = True
        notification_read_node_environment = os.environ.copy()
        notification_read_node_environment[
            "NOTIFICATION_READ_RELATIONS_DATABASE_URL"
        ] = notification_read_url
        notification_read_node_environment["NOTIFICATION_READ_RELATIONS_USER_ID"] = (
            notification_preference_user_id
        )
        notification_read_node_environment[
            "NOTIFICATION_READ_RELATIONS_NOTIFICATION_ID"
        ] = read_relation_notification_id
        notification_read_node_environment[
            "NOTIFICATION_READ_RELATIONS_EXPECTED_ROLE"
        ] = NOTIFICATION_READ_ROLE
        phase = "verify-notification-read-relation-node"
        notification_read_node_result = subprocess.run(
            [node_bin, str(notification_read_node_provider)],
            cwd=notification_read_node_provider.parent,
            env=notification_read_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if notification_read_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(
                phase, notification_read_node_result
            )
            raise RuntimeError("Node Notifications read-relation check failed")
        evidence["notification_read_relations"]["node_relation_check"] = True

        phase = "verify-notification-inbox-api"
        inbox_api_probe = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT count(*)=1 FROM yoao_notifications_v1.inbox_page("
                f"'{notification_preference_user_id}'::uuid,'employee',false,100,0) "
                f"WHERE id='{read_relation_notification_id}'::uuid;"
            ),
        )
        if inbox_api_probe.strip() != "t":
            raise RuntimeError("API LOGIN could not read the inbox owner page")
        evidence["notification_inbox"]["api_function_call"] = True
        for username, password in (
            ("legacy_runtime", runtime_password),
            (NOTIFICATION_INBOX_ROLE, notification_inbox_password),
        ):
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=username,
                password=password,
                database=database,
                statement=(
                    "SELECT * FROM yoao_notifications_v1._visible_inbox_rows("
                    f"'{notification_preference_user_id}'::uuid,'employee')"
                ),
            )
        evidence["notification_inbox"]["private_helper_denied"] = True
        phase = "verify-notification-inbox-node"
        inbox_node_environment = os.environ.copy()
        inbox_node_environment.update(
            {
                "NOTIFICATION_INBOX_DATABASE_URL": notification_inbox_url,
                "NOTIFICATION_INBOX_USER_ID": notification_preference_user_id,
                "NOTIFICATION_INBOX_NOTIFICATION_ID": read_relation_notification_id,
                "NOTIFICATION_INBOX_PLATFORM_ROLE": "employee",
                "NOTIFICATION_INBOX_EXPECTED_ROLE": NOTIFICATION_INBOX_ROLE,
            }
        )
        inbox_node_result = subprocess.run(
            [node_bin, str(inbox_node_provider), "--live"],
            cwd=inbox_node_provider.parent,
            env=inbox_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if inbox_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, inbox_node_result)
            raise RuntimeError("Node Notifications inbox read check failed")
        evidence["notification_inbox"]["node_contract_check"] = True

        phase = "verify-jobs-notification-scope-node"
        jobs_scope_id = str(uuid4())
        jobs_scope_kind = "🧪" * 40
        jobs_scope_snapshot = json.loads(
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=(
                    "INSERT INTO public.async_jobs(id,kind,status,payload,result) VALUES ("
                    f"'{jobs_scope_id}'::uuid,'{jobs_scope_kind}','completed','{{}}'::jsonb,'{{}}'::jsonb) "
                    "RETURNING to_jsonb(async_jobs);"
                ),
            )
        )
        api_jobs_fact = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT row_to_json(fact) FROM yoao_jobs_v1.notification_scope_facts AS fact "
                f"WHERE id='{jobs_scope_id}'::uuid;"
            ),
        )
        if json.loads(api_jobs_fact) != {
            "id": jobs_scope_id,
            "kind": jobs_scope_kind,
            "scope_project_id": None,
        }:
            raise RuntimeError("API LOGIN could not read exact Jobs notification facts")
        evidence["jobs_notification_scope"]["api_fact_call"] = True
        jobs_scope_node_environment = os.environ.copy()
        jobs_scope_node_environment.pop("JOBS_NOTIFICATION_SCOPE_SET_ROLE", None)
        jobs_scope_node_environment.update(
            {
                "JOBS_NOTIFICATION_SCOPE_DATABASE_URL": jobs_notification_scope_url,
                "JOBS_NOTIFICATION_SCOPE_JOB_ID": jobs_scope_id,
                "JOBS_NOTIFICATION_SCOPE_EXPECTED_KIND": jobs_scope_kind,
                "JOBS_NOTIFICATION_SCOPE_EXPECTED_PROJECT_ID": "null",
                "JOBS_NOTIFICATION_SCOPE_EXPECTED_ROLE": JOBS_NOTIFICATION_SCOPE_ROLE,
            }
        )
        jobs_scope_node_result = subprocess.run(
            [node_bin, str(jobs_scope_node_provider), "--live"],
            cwd=jobs_scope_node_provider.parent,
            env=jobs_scope_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if jobs_scope_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, jobs_scope_node_result)
            raise RuntimeError("Node Jobs notification-scope read check failed")
        evidence["jobs_notification_scope"]["node_relation_check"] = True
        evidence["jobs_notification_scope"]["real_login_without_set_role"] = True
        evidence["jobs_notification_scope"]["probe_job_id"] = jobs_scope_id
        evidence["jobs_notification_scope"]["unicode_kind_codepoints"] = 40

        phase = "verify-notification-delivery-read-node"

        async def seed_delivery_project() -> tuple[str, str]:
            from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

            from tests.factory import create_membership, create_project

            engine = create_async_engine(
                URL.create(
                    "postgresql+asyncpg",
                    username="owner",
                    password=owner_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                )
            )
            try:
                async with AsyncSession(engine) as session:
                    actor_id = UUID(notification_preference_user_id)
                    project = await create_project(
                        session, owner_id=actor_id, name="Delivery read Node fixture"
                    )
                    project_id = project.id
                    member = await create_membership(
                        session,
                        project_id=project_id,
                        user_id=actor_id,
                        role="reviewer",
                        assigned_by=actor_id,
                    )
                    member_id = member.id
                    await session.commit()
                    return str(project_id), str(member_id)
            finally:
                await engine.dispose()

        delivery_project_id, delivery_member_id = asyncio.run(seed_delivery_project())
        delivery_node_environment = os.environ.copy()
        delivery_node_environment.update(
            {
                "NOTIFICATION_DELIVERY_DATABASE_URL": notification_delivery_read_url,
                "NOTIFICATION_DELIVERY_USER_ID": notification_preference_user_id,
                "NOTIFICATION_DELIVERY_NOTIFICATION_ID": read_relation_notification_id,
                "NOTIFICATION_DELIVERY_EXPECTED_ROLE": NOTIFICATION_DELIVERY_READ_ROLE,
                "NOTIFICATION_DELIVERY_PROJECT_ID": delivery_project_id,
                "NOTIFICATION_DELIVERY_JOB_ID": jobs_scope_id,
            }
        )
        delivery_node_result = subprocess.run(
            [node_bin, str(delivery_read_node_provider), "--live"],
            cwd=delivery_read_node_provider.parent,
            env=delivery_node_environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if delivery_node_result.returncode:
            node_diagnostic = _node_failure_diagnostic(phase, delivery_node_result)
            raise RuntimeError("Node Notifications delivery-read check failed")
        evidence["notification_delivery_read"]["node_contract_check"] = True
        probe_notification_ids = json.loads(
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=(
                    "SELECT jsonb_agg(id ORDER BY id) FROM public.notifications "
                    "WHERE user_id=:'probe_user_id'::uuid;"
                ),
                variables={"probe_user_id": notification_preference_user_id},
            )
        )
        if not probe_notification_ids or (
            read_relation_notification_id not in probe_notification_ids
        ):
            raise RuntimeError("Notifications preparation missed its read probe")

        phase = "retire-jobs-notification-probe"
        retired_probe = _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql="""
WITH original AS (
    SELECT id FROM public.async_jobs AS job
    WHERE id = :'probe_id'::uuid AND to_jsonb(job) = :'probe_row'::jsonb
    FOR UPDATE
), removed AS (
    DELETE FROM public.async_jobs AS job USING original
    WHERE job.id = original.id
      AND NOT EXISTS (
          SELECT 1 FROM yoao_jobs_v1.dispatch_intents AS intent
          WHERE intent.job_id = job.id
      )
    RETURNING job.id
)
SELECT id FROM removed;
""",
            variables={
                "probe_id": jobs_scope_id,
                "probe_row": json.dumps(jobs_scope_snapshot, ensure_ascii=False),
            },
        )
        if retired_probe.strip() != jobs_scope_id:
            raise RuntimeError("Jobs notification probe changed or was dispatched")
        evidence["jobs_notification_scope"]["unchanged_undispatched_probe_removed"] = (
            True
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
                "mask_gc_role": "yoao_labeling_mask_gc_client",
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
                "result_role": "yoao_preannotation_result_client",
            },
        )

        phase = "install-video-execution-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=video_execution_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "jobs_owner": "jobs_code",
                "video_execution_role": VIDEO_EXECUTION_ROLE,
            },
        )
        phase = "install-labeling-video-writer-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_video_writer_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "labeling_video_writer_role": "yoao_labeling_video_writer_client",
            },
        )
        phase = "install-labeling-task-control-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_task_control_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "task_control_role": "yoao_labeling_task_control_client",
            },
        )
        phase = "install-labeling-annotation-writes-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_annotation_writes_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "annotation_write_role": "yoao_labeling_annotation_write_client",
            },
        )
        phase = "install-labeling-editing-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_editing_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "editing_role": "yoao_labeling_editing_client",
            },
        )
        phase = "install-labeling-batch-control-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_batch_control_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "batch_control_role": "yoao_labeling_batch_control_client",
            },
        )
        phase = "install-labeling-prediction-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_prediction_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "prediction_role": "yoao_labeling_prediction_client",
            },
        )
        phase = "install-labeling-scene-track-commands-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_scene_track_commands_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "scene_track_command_role": "yoao_labeling_scene_track_command_client",
            },
        )
        phase = "install-labeling-cross-frame-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_cross_frame_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "cross_frame_role": "yoao_labeling_cross_frame_client",
            },
        )
        phase = "install-labeling-annotation-maintenance-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_annotation_maintenance_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "annotation_maintenance_role": "yoao_labeling_annotation_maintenance_client",
            },
        )
        phase = "install-labeling-discussion-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_discussion_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "discussion_role": "yoao_labeling_discussion_client",
            },
        )
        phase = "install-labeling-mask-edit-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_mask_edit_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "mask_edit_role": "yoao_labeling_mask_edit_client",
            },
        )
        phase = "install-labeling-scene-read-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_scene_read_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "scene_read_role": "yoao_labeling_scene_read_client",
            },
        )
        phase = "install-labeling-video-control-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_video_control_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "video_control_role": "yoao_labeling_video_control_client",
            },
        )
        phase = "install-labeling-annotation-transfer-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_annotation_transfer_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "annotation_transfer_role": "yoao_labeling_annotation_transfer_client",
            },
        )
        phase = "install-labeling-core-read-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_core_read_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "core_read_role": "yoao_labeling_core_read_client",
            },
        )
        phase = "install-labeling-mask-quality-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_mask_quality_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "quality_role": "yoao_labeling_mask_quality_client",
            },
        )
        phase = "install-labeling-spatial-quality-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_spatial_quality_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "spatial_quality_role": "yoao_labeling_spatial_quality_client",
            },
        )
        phase = "install-labeling-task-maintenance-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_task_maintenance_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "task_maintenance_role": "yoao_labeling_task_maintenance_client",
            },
        )
        phase = "install-inference-pool-reference-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=inference_pool_reference_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "inference_owner": "inference_code",
                "pool_reference_role": "yoao_inference_pool_reference_client",
            },
        )
        phase = "install-video-restore-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=video_restore_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "inference_owner": "inference_code",
                "video_restore_operator_role": "yoao_video_restore_operator_client",
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
        phase = "install-identity-actor-status-grants"
        apply_identity_actor_status_grants()
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
                "identity_key_role": "yoao_identity_key_client",
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
                "identity_session_role": "yoao_identity_session_client",
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
                "identity_account_role": "yoao_identity_account_client",
                "identity_presence_role": "yoao_identity_presence_client",
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
                "identity_group_role": "yoao_identity_group_client",
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
                "identity_deactivation_role": "yoao_identity_deactivation_client",
                "identity_deactivation_operator_role": "yoao_identity_deactivation_operator",
                "notification_publish_role": "yoao_notification_publish_client",
            },
        )
        publisher_role = IDENTITY_CLIENT_ROLES["NOTIFICATION_PUBLISH"]
        publisher_worker_id = str(uuid4())
        publisher_finish_request = json.dumps(
            {
                "schema": "aap-notifications-delivery/v1",
                "notification_id": str(uuid4()),
                "worker_id": publisher_worker_id,
                "lease_id": str(uuid4()),
                "outcome": "delivered",
                "reason": "published",
            },
            separators=(",", ":"),
        )
        publisher_policy = _psql(
            container_id,
            container_name,
            container_identity,
            username=publisher_role,
            password=identity_passwords[publisher_role],
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.identity_delivery_policy() ->> 'policy';"
            ),
        )
        if publisher_policy.strip() != "aap-notifications-delivery/v1":
            raise RuntimeError("Notifications publisher policy is unavailable")
        publisher_claim = _psql(
            container_id,
            container_name,
            container_identity,
            username=publisher_role,
            password=identity_passwords[publisher_role],
            database=database,
            sql=(
                "BEGIN; SELECT yoao_notifications_v1.claim_identity_deliveries("
                "pg_catalog.jsonb_build_object("
                "'schema','aap-notifications-delivery/v1',"
                f"'worker_id','{publisher_worker_id}'::uuid,"
                "'limit',1,'lease_seconds',60)) -> 'data' -> 'items'; ROLLBACK;"
            ),
        )
        if publisher_claim.strip() != "[]":
            raise RuntimeError(
                "Notifications publisher claim probe changed outbox state"
            )
        publisher_finish = _psql(
            container_id,
            container_name,
            container_identity,
            username=publisher_role,
            password=identity_passwords[publisher_role],
            database=database,
            sql=(
                "SELECT yoao_notifications_v1.finish_identity_delivery("
                f"'{publisher_finish_request}'::jsonb) -> 'data' ->> 'accepted';"
            ),
        )
        if publisher_finish.strip() != "false":
            raise RuntimeError("Notifications publisher finish probe was not a no-op")
        for statement in notification_private_statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=publisher_role,
                password=identity_passwords[publisher_role],
                database=database,
                statement=statement,
            )
        evidence["notification_read_relations"][
            "publisher_functions_remain_callable"
        ] = True
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
                "identity_recovery_role": "yoao_identity_recovery_client",
                "identity_mail_role": "yoao_identity_mail_client",
                "identity_mail_settings_role": "yoao_identity_mail_settings_client",
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
                "identity_invitation_role": "yoao_identity_invitation_client",
                "identity_mail_v2_role": "yoao_identity_mail_v2_client",
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
                "identity_directory_role": "yoao_identity_directory_client",
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
                "identity_admin_password_role": "yoao_identity_admin_password_client",
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
                "identity_platform_role_role": "yoao_identity_platform_role_client",
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
                "identity_suspension_role": "yoao_identity_suspension_client",
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
                "identity_deletion_role": "yoao_identity_deletion_client",
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
                "identity_offboarding_role": "yoao_identity_offboarding_client",
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
                "identity_briefs_role": "yoao_identity_briefs_client",
            },
        )
        phase = "install-identity-provisioning-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_provisioning_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "audit_owner": "audit_code",
                "identity_provisioning_role": "yoao_identity_provisioning_client",
            },
        )
        phase = "install-workspace-membership-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_membership_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "labeling_owner": "labeling_code",
                "audit_owner": "audit_code",
                "workspace_membership_role": "yoao_workspace_membership_client",
            },
        )
        phase = "install-workspace-owner-transfer-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_owner_transfer_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "workspace_owner_transfer_role": "yoao_workspace_owner_transfer_client",
            },
        )
        phase = "install-workspace-project-maintenance-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_project_maintenance_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "workspace_owner": "workspace_code",
                "workspace_project_maintenance_role": "yoao_workspace_project_maintenance_client",
            },
        )
        phase = "install-workspace-project-deletion-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_project_deletion_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "workspace_owner": "workspace_code",
                "workspace_project_deletion_role": "yoao_workspace_project_deletion_client",
            },
        )
        phase = "install-workspace-dataset-unlink-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_dataset_unlink_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "workspace_owner": "workspace_code",
                "workspace_dataset_unlink_role": "yoao_workspace_dataset_unlink_client",
            },
        )
        phase = "install-workspace-configuration-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_configuration_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "workspace_configuration_role": "yoao_workspace_configuration_client",
            },
        )
        phase = "install-inference-pipelines-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=inference_pipelines_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "inference_owner": "inference_code",
                "inference_pipelines_role": "yoao_inference_pipelines_client",
            },
        )
        phase = "install-inference-backend-probe-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=inference_backend_probe_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "inference_owner": "inference_code",
                "inference_backend_probe_role": "yoao_inference_backend_probe_client",
            },
        )
        for label, health_grant_sql, variable, role in (
            (
                "observation",
                inference_health_observation_grant_sql,
                "inference_health_observation_role",
                "yoao_inference_health_observation_client",
            ),
            (
                "origin",
                inference_health_origin_grant_sql,
                "inference_health_origin_role",
                "yoao_inference_health_origin_client",
            ),
            (
                "data",
                inference_health_data_grant_sql,
                "inference_health_data_role",
                "yoao_inference_health_data_client",
            ),
        ):
            phase = "install-inference-health-" + label + "-grants"
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=health_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "inference_owner": "inference_code",
                    variable: role,
                },
            )
        phase = "install-inference-registry-ingress-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=inference_registry_ingress_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "inference_owner": "inference_code",
                "inference_registry_ingress_role": INFERENCE_REGISTRY_INGRESS_ROLE,
            },
        )
        phase = "install-inference-routing-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=inference_routing_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "inference_owner": "inference_code",
                "inference_routing_role": INFERENCE_ROUTING_ROLE,
            },
        )
        phase = "install-workspace-template-query-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_template_query_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "workspace_template_query_role": "yoao_workspace_template_query_client",
            },
        )
        phase = "install-workspace-project-query-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_project_query_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "workspace_project_query_role": "yoao_workspace_project_query_client",
            },
        )
        phase = "install-workspace-directory-query-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=workspace_directory_query_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "workspace_directory_query_role": "yoao_workspace_directory_query_client",
            },
        )
        phase = "install-labeling-class-query-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=labeling_class_query_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "labeling_class_query_role": "yoao_labeling_class_query_client",
            },
        )
        resource_environment = backend.environment()
        resource_environment["AAP_MODULE_DATA_PG_DATABASE_OID"] = str(database_oid)
        resource_environment["PLAYWRIGHT_ML_FIXTURE_HOST"] = _fixture_provider_host(
            container_id
        )
        resource_environment["MINIO_ACCESS_KEY"] = backend.settings.minio_access_key
        resource_environment["MINIO_SECRET_KEY"] = backend.settings.minio_secret_key
        if saved_source_profile:
            resource_environment.update(
                AAP_REFERENCE_IO_S3_ENDPOINT=(
                    f"{'https' if backend.settings.minio_use_ssl else 'http'}://"
                    f"{backend.settings.minio_endpoint}"
                ),
                AAP_REFERENCE_IO_S3_ACCESS_KEY=backend.settings.minio_access_key,
                AAP_REFERENCE_IO_S3_SECRET_KEY=backend.settings.minio_secret_key,
                AAP_REFERENCE_IO_SFTP_HOST=resource_environment[
                    "PLAYWRIGHT_ML_FIXTURE_HOST"
                ],
            )
        resource_environment.update(
            {
                (
                    f"AAP_MODULE_DATA_{kind}_URL"
                    if kind.startswith(
                        ("WORKSPACE_", "LABELING_", "INFERENCE_", "VIDEO_")
                    )
                    else f"AAP_MODULE_DATA_IDENTITY_{kind}_URL"
                ): URL.create(
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
                "asset_catalog_role": "yoao_asset_catalog_client",
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
                "asset_source_role": "yoao_asset_source_client",
            },
        )
        if saved_source_profile:
            phase = "install-saved-source-io-grants"
            saved_source_password = secrets.token_hex(24)
            saved_source_installer = (
                ROOT / "infra/docker/source-channel-saved-io-grants.sql"
            )
            saved_source_sql = read_psql_grant_script(saved_source_installer)
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=(
                    f"CREATE ROLE {SAVED_SOURCE_ROLE} LOGIN "
                    f"PASSWORD '{saved_source_password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS;\n"
                    f"{saved_source_sql}"
                ),
                variables={
                    "expected_database": database,
                    "assets_source_saved_io_login": SAVED_SOURCE_ROLE,
                },
            )
            saved_source_url = URL.create(
                "postgresql",
                username=SAVED_SOURCE_ROLE,
                password=saved_source_password,
                host="127.0.0.1",
                port=port,
                database=database,
            ).render_as_string(hide_password=False)

            async def verify_saved_source_login() -> None:
                import asyncpg

                from yoao_assets_source_runtime.saved_io import _PREFLIGHT

                connection = await asyncpg.connect(saved_source_url, timeout=5)
                try:
                    allowed = await connection.fetchval(
                        _PREFLIGHT,
                        list(source_signatures),
                        sorted(
                            {
                                signature.split(".", 1)[0]
                                for signature in source_signatures
                            }
                        ),
                    )
                    login = await connection.fetchrow(
                        "SELECT session_user, oid::bigint, "
                        "pg_catalog.has_database_privilege(current_database(), 'TEMP') "
                        "FROM pg_catalog.pg_database WHERE datname=current_database()"
                    )
                    if allowed is not True or tuple(login) != (
                        SAVED_SOURCE_ROLE,
                        database_oid,
                        False,
                    ):
                        raise RuntimeError("The Source Saved I/O LOGIN profile differs")
                finally:
                    await connection.close()

            asyncio.run(verify_saved_source_login())
            resource_environment["AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"] = (
                saved_source_url
            )
            evidence["roles"].append(SAVED_SOURCE_ROLE)
            evidence["saved_source_io"] = {
                "role": SAVED_SOURCE_ROLE,
                "function_only_login": True,
                "installer_inputs_sha256": {
                    saved_source_installer.relative_to(ROOT).as_posix(): hashlib.sha256(
                        saved_source_sql.encode()
                    ).hexdigest()
                },
                "capabilities": sorted(source_signatures),
            }
        if migration_control_profile:
            phase = "install-storage-migration-control-grants"
            migration_control_password = secrets.token_hex(24)
            migration_control_grant_path = ROOT / "infra/docker/platform-grants.sql"
            migration_control_grant_sql = read_psql_grant_script(
                migration_control_grant_path,
                capability="storage_migration_control",
            )
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=(
                    f"CREATE ROLE {STORAGE_MIGRATION_CONTROL_ROLE} LOGIN "
                    f"PASSWORD '{migration_control_password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS;\n"
                    f"{migration_control_grant_sql}"
                ),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "application_role": IDENTITY_EMBEDDED_API_CONSUMER_ROLE,
                    "storage_migration_control_role": STORAGE_MIGRATION_CONTROL_ROLE,
                },
            )
            migration_control_probe = _psql(
                container_id,
                container_name,
                container_identity,
                username=STORAGE_MIGRATION_CONTROL_ROLE,
                password=migration_control_password,
                database=database,
                sql="""
SELECT current_user = session_user AND session_user = :'role'
    AND pg_catalog.has_function_privilege('yoao_assets_v1.read_migration_window(uuid,text)', 'EXECUTE')
    AND pg_catalog.has_column_privilege('public.storage_migration_cutovers', 'review', 'SELECT')
    AND NOT pg_catalog.has_any_column_privilege('public.storage_connections', 'SELECT,INSERT,UPDATE,REFERENCES');
""",
                variables={"role": STORAGE_MIGRATION_CONTROL_ROLE},
            )
            if migration_control_probe.strip() != "t":
                raise RuntimeError("Storage migration control LOGIN profile differs")
            resource_environment["AAP_MODULE_DATA_STORAGE_MIGRATION_CONTROL_URL"] = (
                URL.create(
                    "postgresql+asyncpg",
                    username=STORAGE_MIGRATION_CONTROL_ROLE,
                    password=migration_control_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False)
            )
            evidence["roles"].append(STORAGE_MIGRATION_CONTROL_ROLE)
            evidence["storage_migration_control"] = {
                "role": STORAGE_MIGRATION_CONTROL_ROLE,
                "grant_path": migration_control_grant_path.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(
                    migration_control_grant_sql.encode()
                ).hexdigest(),
                "connector_storage_denied": True,
            }
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
                "asset_import_role": "yoao_asset_import_client",
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
                "asset_operator_role": "yoao_asset_operator_client",
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
                "asset_media_control_role": "yoao_asset_media_control_client",
                "asset_media_worker_role": "yoao_asset_media_worker_client",
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
                "asset_image_worker_role": "yoao_asset_image_worker_client",
            },
        )
        phase = "install-lan-application-grants"
        lan_profiles = [IDENTITY_API_CONSUMER_ROLE]
        embedded_password = None
        if m2b_profile or selected_test_paths & _EMBEDDED_LOGIN_FILES:
            embedded_role = IDENTITY_EMBEDDED_API_CONSUMER_ROLE
            embedded_password = secrets.token_hex(24)
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=f"CREATE ROLE {embedded_role} LOGIN PASSWORD '{embedded_password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;",
            )
            lan_profiles.append(embedded_role)
            resource_environment[
                "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"
            ] = URL.create(
                "postgresql+asyncpg",
                username=embedded_role,
                password=embedded_password,
                host="127.0.0.1",
                port=port,
                database=database,
            ).render_as_string(hide_password=False)
        for lan_role in lan_profiles:
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=lan_production_grant_sql,
                variables={
                    "expected_database": database,
                    "application_role": lan_role,
                    "migration_role": "owner",
                    "asset_cleanup_role": "yoao_asset_cleanup_client",
                },
            )
        phase = "install-identity-api-consumer-fence-last"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_consumer_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "consumer_role": IDENTITY_API_CONSUMER_ROLE,
                **(
                    {
                        "application_role": IDENTITY_API_CONSUMER_ROLE,
                        "identity_avatar_runtime": "1",
                    }
                    if m2b_profile
                    else {}
                ),
            },
        )
        identity_consumer_notification_fact = _psql(
            container_id,
            container_name,
            container_identity,
            username=IDENTITY_API_CONSUMER_ROLE,
            password=identity_api_consumer_password,
            database=database,
            sql=(
                "SELECT count(*) FROM yoao_notifications_v1.read_notification("
                f"'{read_relation_notification_id}'::uuid) "
                f"WHERE user_id='{notification_preference_user_id}'::uuid "
                f"AND id='{read_relation_notification_id}'::uuid "
                "AND payload = '{\"read_relation\": true}'::jsonb;"
            ),
        )
        if identity_consumer_notification_fact.strip() != "1":
            raise RuntimeError(
                "LAN application role could not read Notifications facts"
            )
        lan_inbox_probe = _psql(
            container_id,
            container_name,
            container_identity,
            username=IDENTITY_API_CONSUMER_ROLE,
            password=identity_api_consumer_password,
            database=database,
            sql=(
                "SELECT EXISTS(SELECT FROM yoao_notifications_v1.inbox_page("
                f"'{notification_preference_user_id}'::uuid,'employee',false,100,0) "
                f"WHERE id='{read_relation_notification_id}'::uuid) "
                "AND yoao_notifications_v1.inbox_total_count("
                f"'{notification_preference_user_id}'::uuid,'employee')>=1 "
                "AND yoao_notifications_v1.inbox_unread_count("
                f"'{notification_preference_user_id}'::uuid,'employee')>=0;"
            ),
        )
        if lan_inbox_probe.strip() != "t":
            raise RuntimeError(
                "LAN application role could not call the three inbox reads"
            )
        evidence["notification_inbox"]["lan_function_calls"] = True
        evidence["notification_delivery_read"]["lan_function_call"] = True
        identity_consumer_audit_fact = _psql(
            container_id,
            container_name,
            container_identity,
            username=IDENTITY_API_CONSUMER_ROLE,
            password=identity_api_consumer_password,
            database=database,
            sql=(
                "SELECT count(*) FROM yoao_audit_v1.audit_events "
                f"WHERE id='{audit_append_id}'::bigint;"
            ),
        )
        if identity_consumer_audit_fact.strip() != "1":
            raise RuntimeError("LAN application role could not read Audit facts")
        for statement in notification_private_statements:
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=IDENTITY_API_CONSUMER_ROLE,
                password=identity_api_consumer_password,
                database=database,
                statement=statement,
            )
        for statement in (
            notification_fact_read_statement,
            *notification_fact_write_statements,
            *audit_fact_write_statements,
        ):
            _assert_psql_permission_denied(
                container_id,
                container_name,
                container_identity,
                username=IDENTITY_API_CONSUMER_ROLE,
                password=identity_api_consumer_password,
                database=database,
                statement=statement,
            )
        if args.workspace_consumer_profile == "strict" and (
            "tests/test_seed_parent_operator_native.py" in selected_test_paths
            or "tests/test_workspace_e2e_native.py" in selected_test_paths
            or "tests/test_identity_e2e_live.py" in selected_test_paths
            or "tests/test_scale_seed_workspace_native.py" in selected_test_paths
            or "tests/test_identity_seed_selection_live.py" in selected_test_paths
            or "tests/test_media_seed_identity_live.py" in selected_test_paths
            or "tests/test_media_seed_workspace_native.py" in selected_test_paths
            or "tests/test_media_seed_workspace_live.py" in selected_test_paths
            or "tests/test_seed_parent_operator_live.py" in selected_test_paths
            or "tests/test_workspace_seed_operator_native.py" in selected_test_paths
            or "tests/test_workspace_seed_operator_live.py" in selected_test_paths
            or _WORKSPACE_ASSEMBLY_TARGET in selected_test_paths
            or (
                "tests/test_workspace_worker_assembly_live.py" in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_WORKER_ASSEMBLY_LIVE") == "1"
            )
            or (
                "tests/test_workspace_cross_frame_worker_live.py" in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_CROSS_FRAME_WORKER_LIVE") == "1"
            )
            or (
                "tests/test_jobs_module_assembly_live.py" in selected_test_paths
                and os.environ.get("AAP_JOBS_HOST_ASSEMBLY_LIVE") == "1"
            )
            or (
                "tests/test_workspace_video_tracker_worker_live.py"
                in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_VIDEO_TRACKER_WORKER_LIVE") == "1"
            )
            or (
                "tests/test_legacy_video_fence_live.py" in selected_test_paths
                and os.environ.get("AAP_LEGACY_VIDEO_FENCE_LIVE") == "1"
            )
            or (
                "tests/test_workspace_export_worker_live.py" in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_EXPORT_WORKER_LIVE") == "1"
            )
        ):
            phase = "install-workspace-api-consumer-fence-last"
            workspace_consumer_path = ROOT / "infra/docker/workspace-grants.sql"
            workspace_consumer_sql = read_psql_grant_script(
                workspace_consumer_path, capability="consumer"
            )
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=workspace_consumer_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "workspace_owner": "workspace_code",
                    "consumer_role": IDENTITY_API_CONSUMER_ROLE,
                },
            )
            evidence["workspace_api_consumer"] = {
                "profile": "strict",
                "role": IDENTITY_API_CONSUMER_ROLE,
                "grant_path": str(workspace_consumer_path.relative_to(ROOT)),
                "sha256": hashlib.sha256(
                    workspace_consumer_sql.encode("utf-8")
                ).hexdigest(),
            }
        elif args.workspace_consumer_profile == "default":
            evidence["workspace_api_consumer"] = {
                "profile": "default",
                "role": IDENTITY_API_CONSUMER_ROLE,
                "grant_path": str(lan_production_grants_path.relative_to(ROOT)),
                "sha256": hashlib.sha256(
                    lan_production_grants_path.read_bytes()
                ).hexdigest(),
            }
        if "workspace_api_consumer" in evidence:
            profile_probe = _psql(
                container_id,
                container_name,
                container_identity,
                username=IDENTITY_API_CONSUMER_ROLE,
                password=identity_api_consumer_password,
                database=database,
                sql=(
                    "SELECT pg_catalog.has_table_privilege(current_user,"
                    "'public.projects','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                    "OR pg_catalog.has_any_column_privilege(current_user,"
                    "'public.projects','SELECT,INSERT,UPDATE,REFERENCES'),"
                    "pg_catalog.has_function_privilege(current_user,"
                    "'yoao_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)',"
                    "'EXECUTE');"
                ),
            ).strip()
            expected_profile = (
                "f|t" if args.workspace_consumer_profile == "default" else "f|f"
            )
            if profile_probe != expected_profile:
                raise RuntimeError(
                    "Workspace consumer credential profile did not persist"
                )
            evidence["workspace_api_consumer"].update(
                {
                    "private_project_storage_denied": True,
                    "fixed_media_seed_capability": (
                        args.workspace_consumer_profile == "default"
                    ),
                }
            )
        if _WORKSPACE_ASSEMBLY_TARGET in selected_test_paths:
            resource_environment["AAP_MODULE_DATA_WORKSPACE_CONSUMER_PROFILE"] = (
                evidence["workspace_api_consumer"]["profile"]
            )
        workspace_import_cli_fence = None
        seed_parent_live = (
            "tests/test_seed_parent_operator_live.py" in selected_test_paths
            and os.environ.get("AAP_SEED_PARENT_OPERATOR_LIVE") == "1"
        )
        workspace_media_seed_live = (
            "tests/test_media_seed_workspace_live.py" in selected_test_paths
            and os.environ.get("AAP_WORKSPACE_MEDIA_SEED_LIVE") == "1"
        )
        seed_operator_live = (
            seed_parent_live
            or workspace_media_seed_live
            or (
                "tests/test_workspace_seed_operator_live.py" in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_SEED_OPERATOR_LIVE") == "1"
            )
        )
        media_seed_identity_live = (
            "tests/test_media_seed_identity_live.py" in selected_test_paths
            and os.environ.get("AAP_MEDIA_SEED_IDENTITY_LIVE") == "1"
        )
        identity_seed_selection_live = (
            "tests/test_identity_seed_selection_live.py" in selected_test_paths
            and os.environ.get("AAP_IDENTITY_SEED_SELECTION_LIVE") == "1"
        )
        identity_e2e_live = (
            "tests/test_identity_e2e_live.py" in selected_test_paths
            and os.environ.get("AAP_IDENTITY_E2E_FIXTURE_LIVE") == "1"
        )
        if identity_e2e_live:

            def workspace_import_cli_fence():
                nonlocal phase
                phase = "install-owner-fixture-api-consumers"
                for profile, capability in (
                    ("identity-consumer-grants.sql", None),
                    ("workspace-grants.sql", "consumer"),
                ):
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql=read_psql_grant_script(
                            ROOT / "infra/docker" / profile,
                            capability=capability,
                        ),
                        variables={
                            "expected_database": database,
                            "migration_role": "owner",
                            "identity_owner": "identity_code",
                            "workspace_owner": "workspace_code",
                            "consumer_role": "legacy_runtime",
                        },
                    )
                evidence["identity_e2e_owner_channel"] = {
                    "runtime_role": "legacy_runtime",
                    "fixture_transport": "apps/web/e2e/fixtures/seed-request.py",
                    "fixture_role": "owner",
                    "runtime_fixture_grants": False,
                }

        elif media_seed_identity_live or identity_seed_selection_live:

            def workspace_import_cli_fence():
                nonlocal phase
                phase = "install-seed-identity-operator"
                persona_grants = ROOT / "infra/docker/identity-grants.sql"
                persona_capability = (
                    "scale_seed" if identity_seed_selection_live else "seed_operator"
                )
                persona_grants_sql = read_psql_grant_script(
                    persona_grants, capability=persona_capability
                )
                for sql in (identity_consumer_grant_sql, persona_grants_sql):
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql=sql,
                        variables={
                            "expected_database": database,
                            "migration_role": "owner",
                            "identity_owner": "identity_code",
                            "consumer_role": "legacy_runtime",
                        },
                    )
                if identity_seed_selection_live:
                    profiles = ["workspace-grants.sql#consumer"]
                    for profile, capability in (("workspace-grants.sql", "consumer"),):
                        _psql(
                            container_id,
                            container_name,
                            container_identity,
                            username="owner",
                            password=owner_password,
                            database=database,
                            sql=read_psql_grant_script(
                                ROOT / "infra/docker" / profile,
                                capability=capability,
                            ),
                            variables={
                                "expected_database": database,
                                "migration_role": "owner",
                                "workspace_owner": "workspace_code",
                                "assets_owner": "assets_code",
                                "labeling_owner": "labeling_code",
                                "consumer_role": "legacy_runtime",
                            },
                        )
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql="""
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.begin_saved_operator_original(jsonb),
    yoao_assets_v1.record_operator_original_prepared(uuid,jsonb),
    yoao_assets_v1.mark_operator_original_writing(uuid),
    yoao_assets_v1.record_operator_original_written(uuid,jsonb),
    yoao_assets_v1.record_operator_original_verified(uuid,jsonb),
    yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    yoao_assets_v1.publish_operator_item_original(jsonb),
    yoao_assets_v1.open_operator_storage_operation(uuid),
    yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)
    TO legacy_runtime;
""",
                    )
                    evidence["workspace_seed_selection_consumer"] = {
                        "role": "legacy_runtime",
                        "workspace_private_tables_denied": True,
                        "profiles": profiles,
                    }
                evidence[
                    "identity_seed_selection_operator"
                    if identity_seed_selection_live
                    else "media_seed_identity_operator"
                ] = {
                    "role": "legacy_runtime",
                    "grant_path": str(persona_grants.relative_to(ROOT)),
                    "sha256": hashlib.sha256(
                        persona_grants_sql.encode("utf-8")
                    ).hexdigest(),
                    "identity_private_tables_denied": True,
                    "ordinary_api_unchanged": True,
                    "other_domains": "existing trusted operator runtime grants retained",
                }

        elif seed_operator_live or (
            "tests/test_workspace_import_cli_live.py" in selected_test_paths
            and os.environ.get("AAP_WORKSPACE_IMPORT_CLI_LIVE") == "1"
        ):

            def workspace_import_cli_fence():
                nonlocal phase
                phase = "install-workspace-import-cli-identity-fence"
                _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="owner",
                    password=owner_password,
                    database=database,
                    sql=identity_consumer_grant_sql,
                    variables={
                        "expected_database": database,
                        "migration_role": "owner",
                        "identity_owner": "identity_code",
                        "consumer_role": "legacy_runtime",
                    },
                )
                phase = "install-workspace-import-cli-workspace-fence"
                workspace_import_grants = ROOT / "infra/docker/workspace-grants.sql"
                workspace_import_grants_sql = read_psql_grant_script(
                    workspace_import_grants, capability="consumer"
                )
                _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="owner",
                    password=owner_password,
                    database=database,
                    sql=workspace_import_grants_sql,
                    variables={
                        "expected_database": database,
                        "migration_role": "owner",
                        "workspace_owner": "workspace_code",
                        "consumer_role": "legacy_runtime",
                    },
                )
                phase = "restore-workspace-import-cli-provisioning-capabilities"
                provisioning_grants = """
    GRANT EXECUTE ON FUNCTION
        yoao_identity_v1.read_provisioning_account(text),
        yoao_identity_v1.create_provisioning_account(jsonb)
        TO legacy_runtime;
    """
                _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="owner",
                    password=owner_password,
                    database=database,
                    sql=provisioning_grants,
                )
                evidence["workspace_import_cli_consumer"] = {
                    "role": "legacy_runtime",
                    "identity_fence": {
                        "path": "infra/docker/identity-consumer-grants.sql",
                        "sha256": hashlib.sha256(
                            identity_consumer_grant_sql.encode()
                        ).hexdigest(),
                    },
                    "workspace_fence": {
                        "path": str(workspace_import_grants.relative_to(ROOT)),
                        "sha256": hashlib.sha256(
                            workspace_import_grants_sql.encode("utf-8")
                        ).hexdigest(),
                    },
                    "provisioning_capabilities": [
                        "yoao_identity_v1.read_provisioning_account(text)",
                        "yoao_identity_v1.create_provisioning_account(jsonb)",
                    ],
                    "provisioning_grants_sha256": hashlib.sha256(
                        provisioning_grants.encode()
                    ).hexdigest(),
                    "other_domains": "existing trusted operator runtime grants retained",
                    "ordinary_api_unchanged": True,
                }
                if seed_operator_live:
                    phase = "install-workspace-seed-operator-capabilities"
                    seed_grants = ROOT / "infra/docker/workspace-grants.sql"
                    seed_grants_sql = read_psql_grant_script(
                        seed_grants, capability="seed_operator"
                    )
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql=seed_grants_sql,
                        variables={
                            "expected_database": database,
                            "migration_role": "owner",
                            "workspace_owner": "workspace_code",
                            "consumer_role": "legacy_runtime",
                        },
                    )
                    evidence["workspace_seed_operator_consumer"] = {
                        "role": "legacy_runtime",
                        "grant_path": str(seed_grants.relative_to(ROOT)),
                        "sha256": hashlib.sha256(
                            seed_grants_sql.encode("utf-8")
                        ).hexdigest(),
                        "ordinary_api_unchanged": True,
                        "project_private_tables_denied": True,
                        "other_domains": "existing trusted operator runtime grants retained",
                    }
                if seed_parent_live or workspace_media_seed_live:
                    phase = "install-identity-seed-operator-capabilities"
                    persona_grants = ROOT / "infra/docker/identity-grants.sql"
                    persona_grants_sql = read_psql_grant_script(
                        persona_grants, capability="seed_operator"
                    )
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql=persona_grants_sql,
                        variables={
                            "expected_database": database,
                            "migration_role": "owner",
                            "identity_owner": "identity_code",
                            "consumer_role": "legacy_runtime",
                        },
                    )
                    evidence["identity_seed_operator_consumer"] = {
                        "role": "legacy_runtime",
                        "grant_path": str(persona_grants.relative_to(ROOT)),
                        "sha256": hashlib.sha256(
                            persona_grants_sql.encode("utf-8")
                        ).hexdigest(),
                        "ordinary_api_unchanged": True,
                    }

        phase = "verify-identity-api-consumer-acl"
        identity_consumer_acl = json.loads(
            _psql(
                container_id,
                container_name,
                container_identity,
                username=IDENTITY_API_CONSUMER_ROLE,
                password=identity_api_consumer_password,
                database=database,
                sql="""
SELECT pg_catalog.jsonb_build_object(
    'views', pg_catalog.jsonb_build_object(
        'account_profiles', pg_catalog.has_table_privilege(
            current_user, 'yoao_identity_v1.account_profiles', 'SELECT'),
        'invitation_audit_targets', pg_catalog.has_table_privilege(
            current_user, 'yoao_identity_v1.invitation_audit_targets', 'SELECT')),
    'functions', (SELECT pg_catalog.jsonb_agg(signature ORDER BY signature) FROM (
        SELECT n.nspname || '.' || p.proname || '(' ||
            pg_catalog.replace(pg_catalog.oidvectortypes(p.proargtypes), ', ', ',') || ')' AS signature
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname ~ '^(aap|yoao)_identity(_[a-z0-9]+)*_v[0-9]+$'
          AND pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')
    ) AS identity_functions),
    'presence_private', NOT (
        pg_catalog.has_table_privilege(
            current_user, 'public.identity_account_presence',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR pg_catalog.has_any_column_privilege(
            current_user, 'public.identity_account_presence',
            'SELECT,INSERT,UPDATE,REFERENCES')
        OR pg_catalog.has_function_privilege(
            current_user,
            'yoao_identity_v1._initialize_account_presence()', 'EXECUTE')
    ),
    'key_usage_private', NOT (
        pg_catalog.has_any_column_privilege(
            current_user, 'public.identity_api_key_usage',
            'SELECT,INSERT,UPDATE,REFERENCES')
        OR pg_catalog.has_table_privilege(
            current_user, 'public.identity_api_key_usage',
            'DELETE,TRUNCATE,TRIGGER')
        OR pg_catalog.has_function_privilege(
            current_user, 'yoao_identity_v1._initialize_api_key_usage()', 'EXECUTE')
    ),
    'preferences_private', NOT (
        pg_catalog.has_table_privilege(
            current_user, 'public.identity_account_preferences',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR pg_catalog.has_any_column_privilege(
            current_user, 'public.identity_account_preferences',
            'SELECT,INSERT,UPDATE,REFERENCES')
        OR pg_catalog.has_function_privilege(
            current_user,
            'yoao_identity_v1._initialize_account_preferences()', 'EXECUTE')
    )
)::text;
""",
            ).strip()
        )
        expected_identity_functions = (
            (
                *IDENTITY_ISSUER_FACT_FUNCTIONS,
                *IDENTITY_AVATAR_FUNCTIONS,
                *IDENTITY_AVATAR_INVENTORY_FUNCTIONS,
            )
            if m2b_profile
            else IDENTITY_ISSUER_FACT_FUNCTIONS
        )
        if identity_consumer_acl["views"] != {
            "account_profiles": True,
            "invitation_audit_targets": True,
        } or identity_consumer_acl["functions"] != sorted(expected_identity_functions):
            raise RuntimeError(
                "Identity API consumer's exact capability profile differs"
            )
        if not identity_consumer_acl["presence_private"]:
            raise RuntimeError(
                "Identity API consumer can access private presence storage"
            )
        if not identity_consumer_acl["key_usage_private"]:
            raise RuntimeError(
                "Identity API consumer can access private key usage storage"
            )
        if not identity_consumer_acl["preferences_private"]:
            raise RuntimeError(
                "Identity API consumer can access private preferences storage"
            )
        evidence["identity_api_consumer"] = {
            "role": IDENTITY_API_CONSUMER_ROLE,
            "avatar_runtime_capabilities": m2b_profile,
            "grant_order": [
                {
                    "path": "infra/docker/lan-production-grants.sql",
                    "sha256": hashlib.sha256(
                        lan_production_grant_sql.encode()
                    ).hexdigest(),
                },
                {
                    "path": "infra/docker/identity-consumer-grants.sql",
                    "sha256": hashlib.sha256(
                        identity_consumer_grant_sql.encode()
                    ).hexdigest(),
                },
            ],
            "effective_access": identity_consumer_acl,
        }
        resource_environment.update(
            {
                "AAP_MODULE_DATA_IDENTITY_AVATAR_RUNTIME": "1" if m2b_profile else "",
                "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL": URL.create(
                    "postgresql+asyncpg",
                    username=IDENTITY_API_CONSUMER_ROLE,
                    password=identity_api_consumer_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_CONTAINER_URL": URL.create(
                    "postgresql+asyncpg",
                    username=IDENTITY_API_CONSUMER_ROLE,
                    password=identity_api_consumer_password,
                    host="host.docker.internal",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
            }
        )
        phase = "create-scoped-storage-identity"
        for bucket in {
            resources["buckets"]["MINIO_DATASETS_BUCKET"],
            resources["buckets"]["MINIO_BUCKET"],
        }:
            _enable_owned_bucket_versioning(backend, bucket)
        if not collect_only and physical_retirement_profile:
            image_import_storage = _cleanup_storage_scope(backend)
            image_import_storage["datasets_bucket"] = resources["buckets"][
                "MINIO_DATASETS_BUCKET"
            ]
            from worktree_env import atomic_json, state_path

            journal_path = state_path(
                resource_root, "test", "legacy-original-retirement.json"
            )
            if journal_path.exists():
                raise RuntimeError("Image-import retirement journal already exists")
            image_import_storage["journal_path"] = journal_path
            image_import_storage["journal"] = {
                "schema": 1,
                "resources": resources,
                "endpoint": backend.settings.minio_endpoint,
                "use_ssl": backend.settings.minio_use_ssl,
                "bucket": image_import_storage["datasets_bucket"],
                "container": image_import_storage["container"],
                "access_key": image_import_storage["access_key"],
                "secret_key": image_import_storage["secret_key"],
                "policy": image_import_storage["policy"],
                "config": image_import_storage["config"],
                "phase": "identity_creating",
            }
            atomic_json(journal_path, image_import_storage["journal"], exclusive=True)
            _create_image_import_storage_scope(image_import_storage)
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
        if native_provider_owner is not None:
            phase = "provision-native-provider"
            native_provider_scopes = provision_native_provider(
                native_provider_owner,
                native_provider_state,
                resources,
                {
                    "images_control": media_control_storage,
                    "images_worker": image_worker_storage,
                    "cleanup": cleanup_storage,
                },
                evidence["native_provider"],
            )
            native_source_storage = native_provider_scopes["native_source"]
            native_connection_storage = native_provider_scopes["native_connection"]
        else:
            native_source_storage = _native_storage_scope(backend)
            _create_native_storage_scope(
                native_source_storage,
                _native_source_storage_policy(native_source_storage),
            )
            native_connection_storage = _native_storage_scope(backend)
            native_connection_storage["bucket"] = resources["buckets"]["MINIO_BUCKET"]
            native_connection_storage["policy"] = (
                "aap-native-connection-" + secrets.token_hex(8)
            )
            _create_native_storage_scope(
                native_connection_storage,
                _native_connection_storage_policy(native_connection_storage),
            )
        native_signer_tokens = {
            "writer": secrets.token_urlsafe(48),
            "reader": secrets.token_urlsafe(48),
            "cleanup": secrets.token_urlsafe(48),
        }
        if len(set(native_signer_tokens.values())) != 3 or any(
            token == source_storage["access_key"]
            for token in native_signer_tokens.values()
        ):
            raise RuntimeError("Native signer role tokens are not distinct")
        if (
            native_source_storage["access_key"]
            == native_connection_storage["access_key"]
            or native_source_storage["access_key"] == source_storage["access_key"]
            or native_connection_storage["access_key"] == source_storage["access_key"]
        ):
            raise RuntimeError("Native S3 identities are not distinct")
        if native_provider_owner is None:
            phase = "verify-native-storage-scopes"
            evidence["native_storage_s3"] = _verify_native_storage_scopes(
                backend,
                native_source_storage,
                native_connection_storage,
                cleanup_storage,
            )

        audit_cli_live = (
            "tests/test_audit_maintenance_module_live.py" in selected_test_paths
            and os.environ.get("AAP_AUDIT_MAINTENANCE_MODULE_ACCEPTANCE") == "1"
        )
        audit_worker_live = (
            "tests/test_audit_maintenance_consumers_cold.py" in selected_test_paths
            and os.environ.get("AAP_AUDIT_MAINTENANCE_CONSUMER_ACCEPTANCE") == "1"
        )
        if audit_cli_live or audit_worker_live:
            bucket = resources["buckets"]["MINIO_AUDIT_ARCHIVE_BUCKET"]
            tags = backend.s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
            if not any(
                tag["Key"] == "yoao-worktree-owner"
                and tag["Value"] == resources["owner"]
                for tag in tags
            ):
                raise RuntimeError("Audit archive bucket ownership differs")
            audit_archive_writer_storage = _cleanup_storage_scope(backend)
            audit_archive_writer_storage.update(
                bucket=bucket, policy="aap-audit-archive-writer-" + secrets.token_hex(8)
            )
            _create_audit_archive_storage_scope(
                audit_archive_writer_storage, "s3:PutObject"
            )
            audit_archive_reader_storage = _cleanup_storage_scope(backend)
            audit_archive_reader_storage.update(
                bucket=bucket, policy="aap-audit-archive-reader-" + secrets.token_hex(8)
            )
            _create_audit_archive_storage_scope(
                audit_archive_reader_storage, "s3:GetObject"
            )
            if audit_worker_live:
                audit_archive_platform_storage = _cleanup_storage_scope(backend)
                audit_archive_platform_storage.update(
                    bucket=bucket,
                    policy="aap-audit-archive-platform-" + secrets.token_hex(8),
                )
                _create_audit_archive_storage_scope(
                    audit_archive_platform_storage, "s3:PutObject", "s3:GetObject"
                )
            evidence["audit_archive_storage"] = {
                "bucket": bucket,
                "separate_writer_and_reader_provisioned": True,
                "platform_readback_provisioned": audit_worker_live,
            }
        if selected_test_paths & {
            "tests/test_labeling_video_writer_live.py",
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
        if selected_test_paths & {
            "tests/test_video_consumers_containers.py",
            "tests/test_identity_module_assembly_live.py",
            "tests/test_notifications_socket_runtime_live.py",
            "tests/test_notifications_module_assembly_live.py",
            "tests/test_jobs_module_assembly_live.py",
            "tests/test_identity_e2e_live.py",
            "tests/test_identity_embedded_acl_live.py",
            "tests/test_worktree_identity_runtime_role_live.py",
            "tests/test_labeling_module_api_cold.py",
            "tests/test_audit_consumers_cold.py",
        }:
            api_bootstrap_storage = _cleanup_storage_scope(backend)
            if api_bootstrap_storage["container"] != cleanup_storage["container"]:
                raise RuntimeError("API bootstrap and verifier MinIO identities differ")
            api_bootstrap_storage["buckets"] = resources["buckets"]
            api_bootstrap_storage["policy"] = "aap-api-bootstrap-" + secrets.token_hex(
                8
            )
            _create_api_bootstrap_storage_scope(
                api_bootstrap_storage, e2e_fixtures=identity_e2e_live
            )
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
        phase = "install-inference-owner-profiles"
        inference_owner_grant_sql = read_psql_grant_script(
            ROOT / "infra/docker/inference-grants.sql", capability="owner"
        )
        for label, (profile, role) in INFERENCE_OWNER_PROFILES.items():
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=inference_owner_grant_sql,
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "inference_owner": "inference_code",
                    "client_role": role,
                    "profile": profile,
                },
            )
            resource_environment[f"AAP_MODULE_DATA_{label}_URL"] = URL.create(
                "postgresql+asyncpg",
                username=role,
                password=inference_owner_passwords[label],
                host="127.0.0.1",
                port=port,
                database=database,
            ).render_as_string(hide_password=False)
        evidence["inference_owner_profiles"] = {
            label: {"profile": profile, "role": role}
            for label, (profile, role) in INFERENCE_OWNER_PROFILES.items()
        }

        resource_environment.update(
            {
                "AAP_MODULE_DATA_ROLE_ACCEPTANCE": "1",
                "AAP_IDENTITY_BRIEFS_LIVE_ACCEPTANCE": "1",
                "AAP_WORKSPACE_OWNER_TRANSFER_LIVE": "1",
                "RUN_NATIVE_IMPORT_DELIVERY_LEASE_TEST": "1",
                "AAP_MODULE_DATA_APPEND_URL": append_url,
                "AAP_MODULE_DATA_AUDIT_MAINTENANCE_URL": audit_maintenance_url,
                "AAP_MODULE_DATA_AUDIT_APPEND_URL": make_url(audit_append_url)
                .set(drivername="postgresql+asyncpg")
                .render_as_string(hide_password=False),
                "AAP_MODULE_DATA_NOTIFICATION_DELIVERY_READ_URL": notification_delivery_read_url,
                "AAP_MODULE_DATA_JOBS_LIST_QUERY_URL": jobs_list_query_url,
                "AAP_MODULE_DATA_JOBS_LIFECYCLE_URL": jobs_lifecycle_url,
                "AAP_MODULE_DATA_JOBS_DISPATCH_RELAY_URL": jobs_dispatch_relay_url,
                "AAP_MODULE_DATA_JOBS_PUBLICATION_URL": jobs_publication_url,
                "AAP_MODULE_DATA_EXPORT_EXECUTION_URL": export_execution_url,
                "AAP_MODULE_DATA_ASSET_CLEANUP_URL": cleanup_url,
                "AAP_MODULE_DATA_ASSET_CATALOG_URL": catalog_url,
                "AAP_MODULE_DATA_ASSET_SOURCE_URL": source_url,
                "AAP_MODULE_DATA_ASSET_IMPORT_URL": import_url,
                "AAP_MODULE_DATA_ASSET_OPERATOR_URL": operator_url,
                "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL": media_control_url,
                "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL": media_worker_url,
                "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_URL": image_worker_url,
                "AAP_MODULE_DATA_INFERENCE_REGISTRY_INGRESS_URL": URL.create(
                    "postgresql+asyncpg",
                    username=INFERENCE_REGISTRY_INGRESS_ROLE,
                    password=inference_registry_ingress_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_INFERENCE_ROUTING_URL": URL.create(
                    "postgresql+asyncpg",
                    username=INFERENCE_ROUTING_ROLE,
                    password=inference_routing_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL": URL.create(
                    "postgresql+asyncpg",
                    username="yoao_preannotation_result_client",
                    password=result_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_VIDEO_EXECUTION_URL": URL.create(
                    "postgresql+asyncpg",
                    username=VIDEO_EXECUTION_ROLE,
                    password=video_execution_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_LABELING_MASK_GC_URL": URL.create(
                    "postgresql+asyncpg",
                    username="yoao_labeling_mask_gc_client",
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
                "AAP_MODULE_DATA_MINIO_CONTAINER_ID": cleanup_storage["container"],
                "AAP_MODULE_DATA_REDIS_CONTAINER_NAME": resources["redis_name"],
                "AAP_MODULE_DATA_REDIS_OWNER": resources["owner"],
                "AAP_MODULE_DATA_PG_CONTAINER_NAME": container_name,
                "AAP_MODULE_DATA_PG_CONTAINER_IDENTITY": container_identity,
                NATIVE_SOURCE_S3_ACCESS_KEY_SETTING: native_source_storage[
                    "access_key"
                ],
                NATIVE_SOURCE_S3_SECRET_KEY_SETTING: native_source_storage[
                    "secret_key"
                ],
                NATIVE_CONNECTION_S3_ACCESS_KEY_SETTING: native_connection_storage[
                    "access_key"
                ],
                NATIVE_CONNECTION_S3_SECRET_KEY_SETTING: native_connection_storage[
                    "secret_key"
                ],
                NATIVE_SOURCE_WRITER_TOKEN_SETTING: native_signer_tokens["writer"],
                NATIVE_SOURCE_READER_TOKEN_SETTING: native_signer_tokens["reader"],
                NATIVE_SOURCE_CLEANUP_TOKEN_SETTING: native_signer_tokens["cleanup"],
                "AAP_MODULE_DATA_NATIVE_B_BUCKET": native_connection_storage["bucket"],
                "AAP_MODULE_DATA_NATIVE_B_PREFIX": NATIVE_CONNECTION_BASE_PREFIX,
                "AAP_MODULE_DATA_NATIVE_SOURCE_BUCKET": native_source_storage["bucket"],
            }
        )
        evidence["native_signer"] = {
            "roles": ["writer", "reader", "cleanup"],
            "tokens_distinct": True,
            "separate_native_s3_identity": True,
            "separate_connection_s3_identity": True,
        }
        if native_provider_owner is not None:
            native_environment = native_profile_environment(
                native_provider_owner,
                native_provider_state,
                native_provider_scopes,
                f"{'https' if settings.minio_use_ssl else 'http'}://"
                f"{settings.minio_endpoint}",
                legacy_admin_access_key=settings.minio_access_key,
            )
            native_process_environment_path = (
                native_provider_owner.private / "native-process-env.json"
            )
            native_process_environment_bytes = json.dumps(native_environment).encode()
            private_write(
                native_process_environment_path, native_process_environment_bytes
            )
            native_process_environment_sha256 = sha256(native_process_environment_bytes)
        if image_import_storage is not None:
            resource_environment.update(
                {
                    "AAP_IMAGE_IMPORT_S3_ACCESS_KEY": image_import_storage[
                        "access_key"
                    ],
                    "AAP_IMAGE_IMPORT_S3_SECRET_KEY": image_import_storage[
                        "secret_key"
                    ],
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
            ("WRITE", audit_archive_writer_storage),
            ("READ", audit_archive_reader_storage),
            ("PLATFORM", audit_archive_platform_storage),
        ):
            if scope is not None:
                resource_environment.update(
                    {
                        f"AAP_MODULE_DATA_AUDIT_ARCHIVE_{label}_ACCESS_KEY": scope[
                            "access_key"
                        ],
                        f"AAP_MODULE_DATA_AUDIT_ARCHIVE_{label}_SECRET_KEY": scope[
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
            "import json,os,sys\n"
            "args=sys.argv[1:]\n"
            "os.chdir(args.pop(0))\n"
            "manifest=args.pop(0)\n"
            "native_env=args.pop(0)\n"
            "native_sha=args.pop(0)\n"
            "if native_env:\n"
            "    from pathlib import Path\n"
            "    from hashlib import sha256\n"
            "    raw=Path(native_env).read_bytes()\n"
            "    if sha256(raw).hexdigest() != native_sha:\n"
            "        raise RuntimeError('Native process environment identity changed')\n"
            "    os.environ.update(json.loads(raw))\n"
            "sys.path.insert(0, os.getcwd())\n"
            "import pytest\n"
            "class CollectionManifest:\n"
            "    def pytest_collection_finish(self, session):\n"
            "        with open(manifest, 'w', encoding='utf-8') as output:\n"
            "            json.dump([item.nodeid for item in session.items], output)\n"
            "raise SystemExit(pytest.main(args, plugins=[CollectionManifest()]))\n",
            str(ROOT / "apps/api"),
            str(collection_manifest),
            str(native_process_environment_path or ""),
            native_process_environment_sha256,
            "-q" if collect_only else "-v",
            "--tb=short",
            *(["--collect-only"] if collect_only else [f"--junitxml={junit}"]),
            *test_files,
        ]
        options = parse_arguments(["exec", "--mode", "test", "--", *command])
        phase = "pytest-collection" if collect_only else "pytest"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        with patch.dict(os.environ, resource_environment, clear=False):
            prepare_owned_resources = backend.prepare

            def prepare_fenced_resources(*, skip_migrations=False):
                nonlocal phase
                prepare_owned_resources(skip_migrations=skip_migrations)
                if workspace_import_cli_fence is not None:
                    workspace_import_cli_fence()
                apply_notification_preferences_grants()
                apply_notification_append_grants()
                apply_notification_delivery_read_grants()
                apply_notification_inbox_grants()
                apply_jobs_list_query_grants()
                apply_jobs_lifecycle_grants()
                apply_jobs_dispatch_grants()
                apply_execution_grants()
                apply_identity_actor_status_grants()
                apply_audit_append_grants()
                apply_notification_read_grants(NOTIFICATION_READ_ROLE)
                apply_audit_read_grants()
                apply_audit_maintenance_grants()
                apply_jobs_scope_grants()
                video_request_owner = _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="owner",
                    password=owner_password,
                    database=database,
                    sql="""
SELECT EXISTS (
    SELECT FROM pg_catalog.pg_proc AS fn
    JOIN pg_catalog.pg_trigger AS trigger ON trigger.tgfoid = fn.oid
    WHERE fn.oid = 'yoao_assets_v1.prepare_fixture_video_request_delivery()'::regprocedure
      AND fn.proowner = 'jobs_code'::regrole AND fn.prosecdef
      AND fn.proconfig = ARRAY['search_path=pg_catalog, pg_temp']
      AND NOT pg_catalog.has_schema_privilege(fn.proowner, fn.pronamespace, 'CREATE')
      AND trigger.tgrelid = 'public.video_derivative_outputs'::regclass
      AND trigger.tgname = 'fixture_video_request_delivery' AND trigger.tgqual IS NOT NULL
      AND NOT EXISTS (
          SELECT FROM pg_catalog.aclexplode(
              COALESCE(fn.proacl, pg_catalog.acldefault('f', fn.proowner))
          ) AS entry
          WHERE entry.grantee <> fn.proowner AND entry.privilege_type = 'EXECUTE'
      )
);
""",
                )
                if video_request_owner.strip() != "t":
                    raise RuntimeError(
                        "Fixture video request trigger owner or ACL differs"
                    )
                private_fixture_statements = (
                    "SELECT * FROM yoao_fixture_v1.fixture_runs LIMIT 1",
                    "SELECT * FROM yoao_fixture_v1.fixture_native_media_work_intents LIMIT 1",
                    "SELECT yoao_assets_v1.fixture_native_admission_current('media',NULL::uuid,NULL::uuid)",
                    "SELECT yoao_assets_v1.fixture_screenshot_output_receipt(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text)",
                    "SELECT * FROM yoao_assets_v1.fixture_screenshot_item_facts LIMIT 1",
                    "SELECT yoao_labeling_v1.screenshot_operations_command('{}'::jsonb)",
                    "SELECT yoao_identity_invitations_v1.bind_fixture_invitation_birth(NULL::uuid,NULL::uuid)",
                    "SELECT yoao_identity_invitations_v1.lock_fixture_invitation_run(NULL::uuid)",
                    "SELECT yoao_labeling_v1._fixture_screenshot_user(NULL::uuid,NULL::text,NULL::text)",
                    "SELECT yoao_labeling_v1.create_scale_seed_tasks(NULL::text,NULL::uuid,NULL::uuid)",
                    "SELECT yoao_jobs_v1.delete_fixture_project_jobs(ARRAY[]::uuid[])",
                    "SELECT job_id FROM yoao_jobs_v1.fixture_annotation_imports LIMIT 1",
                    "UPDATE yoao_jobs_v1.fixture_annotation_imports SET result = NULL WHERE false",
                    "SELECT yoao_jobs_v1.fixture_real_job_retired(NULL::uuid,NULL::uuid,NULL::uuid)",
                    "SELECT yoao_jobs_v1.observe_fixture_prediction_import()",
                    "SELECT yoao_jobs_v1.delete_fixture_media_backfills(NULL::uuid,NULL::uuid)",
                    "SELECT yoao_jobs_v1.delete_fixture_presentation_jobs(NULL::uuid,NULL::uuid,ARRAY[]::uuid[])",
                    "SELECT yoao_labeling_v1.guard_fixture_presentation_failures(NULL::uuid,NULL::uuid,ARRAY[]::uuid[])",
                    "SELECT yoao_labeling_v1.guard_fixture_project_retry_anchors(ARRAY[]::uuid[])",
                    "SELECT yoao_assets_v1.prepare_fixture_video_request_delivery()",
                    "SELECT yoao_jobs_v1.fixture_video_request_delivery_matches(NULL::uuid,NULL::uuid)",
                    "SELECT yoao_jobs_v1.next_fixture_video_request_publication(NULL::uuid)",
                    "SELECT yoao_jobs_v1.begin_fixture_video_request_publication(NULL::uuid,NULL::uuid,NULL::uuid)",
                    "SELECT yoao_jobs_v1.finish_fixture_video_request_publication(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text)",
                    "SELECT yoao_jobs_v1.begin_fixture_media_publication(NULL::uuid,NULL::uuid)",
                    "SELECT delivery_id FROM yoao_jobs_v1.fixture_media_deliveries LIMIT 1",
                    "SELECT delivery_id FROM yoao_jobs_v1.fixture_media_publication_attempts LIMIT 1",
                )
                for statement in (
                    *notification_private_statements,
                    notification_fact_read_statement,
                    *private_fixture_statements,
                ):
                    _assert_psql_permission_denied(
                        container_id,
                        container_name,
                        container_identity,
                        username="legacy_runtime",
                        password=runtime_password,
                        database=database,
                        statement=statement,
                    )
                evidence["private_fixture_runtime_boundary"] = {
                    "role": "legacy_runtime",
                    "video_request_trigger_owner": "jobs_code",
                    "statements": list(private_fixture_statements),
                    "sqlstate": "42501",
                    "after_launcher_prepare": True,
                }
                notification_fact = _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="legacy_runtime",
                    password=runtime_password,
                    database=database,
                    sql=(
                        "SELECT count(*) FROM "
                        "yoao_notifications_v1.read_notification("
                        f"'{read_relation_notification_id}'::uuid) "
                        f"WHERE id='{read_relation_notification_id}'::uuid "
                        "AND payload = '{\"read_relation\": true}'::jsonb;"
                    ),
                )
                audit_fact = _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="legacy_runtime",
                    password=runtime_password,
                    database=database,
                    sql=(
                        "SELECT count(*) FROM yoao_audit_v1.audit_events "
                        f"WHERE id='{audit_append_id}'::bigint;"
                    ),
                )
                if notification_fact.strip() != "1" or audit_fact.strip() != "1":
                    raise RuntimeError(
                        "module test preparation lost an owner read relation grant"
                    )
                evidence["audit_maintenance"].update(
                    _verify_audit_maintenance_boundary(
                        container_id,
                        container_name,
                        container_identity,
                        database=database,
                        owner_password=owner_password,
                        runtime_password=runtime_password,
                        maintenance_password=audit_maintenance_password,
                        maintenance_url=audit_maintenance_url,
                    )
                )
                evidence["audit_maintenance"]["after_launcher_prepare"] = True
                if m2b_profile:
                    phase = "verify-three-ordinary-application-logins"
                    evidence["ordinary_profiles"] = {}
                    for profile, username, password, installer in (
                        (
                            "lan-embedded",
                            IDENTITY_EMBEDDED_API_CONSUMER_ROLE,
                            embedded_password,
                            ["infra/docker/lan-production-grants.sql"],
                        ),
                        (
                            "lan-remote-issuer",
                            IDENTITY_API_CONSUMER_ROLE,
                            identity_api_consumer_password,
                            [
                                grant["path"]
                                for grant in evidence["identity_api_consumer"][
                                    "grant_order"
                                ]
                            ],
                        ),
                        (
                            "worktree-runtime",
                            "legacy_runtime",
                            runtime_password,
                            [
                                "scripts/worktree_resources.py::LocalResources.grant_runtime"
                            ],
                        ),
                    ):
                        access = _ordinary_login_acl(
                            container_id,
                            container_name,
                            container_identity,
                            username=username,
                            password=password,
                            database=database,
                            source_signatures=source_signatures,
                        )
                        evidence["ordinary_profiles"][profile] = {
                            "installers": installer,
                            **access,
                        }
                        if (
                            access["login"] != username
                            or access["effective_role"] != username
                            or access["database_oid"] != database_oid
                            or access["restricted_login"] is not True
                            or not access["scanned_private_relations"]
                            or not access["scanned_private_sequences"]
                            or access["unexpected_private_relations"]
                            or access["unexpected_private_sequences"]
                            or access["unexpected_private_functions"]
                            or access["missing_business_functions"]
                        ):
                            raise RuntimeError(
                                f"Unexpected {profile} application LOGIN access"
                            )
                phase = "retire-notification-preparation"
                _container_details(
                    container_id,
                    container_name,
                    container_identity,
                    require_running=True,
                )
                evidence[
                    "image_import_preparation_retirement"
                    if not collect_only and physical_retirement_profile
                    else "notification_preparation_retirement"
                ] = asyncio.run(
                    _retire_notification_preparation(
                        backend,
                        database_oid,
                        notification_preference_user_id,
                        delivery_project_id,
                        delivery_member_id,
                        probe_notification_ids,
                        require_empty=not collect_only and physical_retirement_profile,
                    )
                )
                phase = "pytest-collection" if collect_only else "pytest"
                _write_evidence(output, evidence)

            with patch.object(backend, "prepare", prepare_fenced_resources):
                test_exit_code = run_processes(backend, options)
        evidence["test_exit_code"] = test_exit_code
        if test_exit_code == 0:
            collected = json.loads(collection_manifest.read_text(encoding="utf-8"))
            evidence["collected_test_nodes"] = collected
            if not collect_only:
                evidence["passed_test_count"] = _passed_test_count(
                    junit, test_files, collected
                )
        exit_code = test_exit_code
        evidence["status"] = (
            "collected"
            if collect_only and exit_code == 0
            else "passed"
            if exit_code == 0
            else "failed"
        )
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = {"phase": phase, "error_type": type(error).__name__}
        if phase.startswith("native-provider") or phase == "provision-native-provider":
            from storage_s3_contract import safe_error

            evidence["failure"]["error_code"] = safe_error(error)
            if (
                native_provider_owner is not None
                and native_provider_owner.private.exists()
            ):
                private_write(
                    native_provider_owner.private / "native-acceptance-failure.json",
                    json.dumps(
                        {"type": type(error).__name__, "detail": str(error)}
                    ).encode(),
                )
        if isinstance(error, ModuleNotFoundError):
            evidence["failure"]["module"] = error.name
        if isinstance(error, RuntimeError) and (
            str(error).startswith("psql acceptance phase failed with exit ")
            or (
                phase == "preflight"
                and str(error).startswith("Missing module-data acceptance cases: ")
            )
            or (phase.startswith("pytest") and str(error).startswith("Acceptance "))
        ):
            evidence["failure"]["diagnostic"] = str(error)
        if node_diagnostic is not None:
            evidence["failure"]["node_diagnostic"] = node_diagnostic
        exit_code = 1
    except BaseException as error:
        exit_code = 1
        evidence["status"] = "failed"
        evidence["failure"] = {"phase": phase, "error_type": type(error).__name__}
        raise
    finally:
        local_resources_destroyed = backend is None or not backend.touched
        if backend is not None and backend.touched:
            try:
                if native_lifecycle_profile and exit_code != 0:
                    evidence["cleanup"]["retention_reason"] = (
                        "failed_native_lifecycle_runtime"
                    )
                    raise RuntimeError(
                        "Failed Native lifecycle retained for explicit retirement approval"
                    )
                if not collect_only and physical_retirement_profile and exit_code != 0:
                    evidence["cleanup"]["retention_reason"] = (
                        "failed_image_import_runtime"
                    )
                    raise RuntimeError("Retain the failed image-import runtime")
                if args.retain_failed_runtime and exit_code != 0:
                    evidence["cleanup"]["retention_reason"] = "requested_failed_runtime"
                    raise RuntimeError(
                        "Failed diagnostic run retained for explicit owner inspection"
                    )
                if container_id:
                    _container_details(
                        container_id,
                        container_name,
                        container_identity,
                        require_running=True,
                    )
                    database_catalog = json.loads(
                        _psql(
                            container_id,
                            container_name,
                            container_identity,
                            username="owner",
                            password=owner_password,
                            database="postgres",
                            sql=(
                                "SELECT COALESCE(json_agg(json_build_object("
                                "'name', datname, 'oid', oid::bigint) ORDER BY datname), "
                                "'[]'::json) FROM pg_database "
                                "WHERE NOT datistemplate AND datname <> 'postgres';"
                            ),
                        )
                    )
                    retained_databases = [
                        item
                        for item in database_catalog
                        if item["name"] != backend.resources["database"]
                    ]
                    evidence["cleanup"]["retained_databases"] = retained_databases
                    if retained_databases:
                        raise RuntimeError(
                            "Child or unknown databases remain; retain the "
                            "enclosing PostgreSQL, storage and credentials"
                        )
                if not collect_only and physical_retirement_profile:
                    from worktree_env import atomic_json, read_state

                    scope = image_import_storage
                    if not container_id or scope is None:
                        raise RuntimeError(
                            "Image-import runtime identity is unavailable"
                        )
                    journal, journal_path = scope["journal"], scope["journal_path"]
                    if (
                        read_state(journal_path) != journal
                        or journal.get("phase") != "identity_ready"
                        or journal.get("resources") != backend.resources
                    ):
                        raise RuntimeError("Image-import ownership journal changed")
                    with backend.fixture_authority_fence():
                        with backend._prepare_owned_destruction(
                            backend.resources["confirmation"]
                        ) as prepared:
                            evidence["cleanup"]["legacy_storage_guard_probe"] = (
                                asyncio.run(_probe_image_import_guard(backend, scope))
                            )
                            journal["phase"] = "gate_fencing"
                            atomic_json(journal_path, journal)
                            proof = asyncio.run(
                                _fence_image_import_legacy_storage(backend)
                            )
                            if proof["database_oid"] != database_oid:
                                raise RuntimeError(
                                    "Managed-original database birth changed"
                                )
                            if (
                                "tests/test_identity_seed_selection_live.py"
                                in selected_test_paths
                            ):
                                creations = proof["verified_creations"]
                                if (
                                    len(creations) != 4
                                    or len({row["key"] for row in creations}) != 4
                                ):
                                    raise RuntimeError(
                                        "Scale-seed verified original ledger is incomplete"
                                    )
                            databases = [
                                item
                                for item in database_catalog
                                if item["name"] == backend.resources["database"]
                            ]
                            if (
                                len(databases) != 1
                                or databases[0]["oid"] != proof["database_oid"]
                            ):
                                raise RuntimeError(
                                    "Image-import database birth changed"
                                )
                            journal["fence"] = proof
                            journal["phase"] = "gate_fenced"
                            atomic_json(journal_path, journal)
                            evidence["cleanup"]["legacy_storage_fence"] = proof
                            journal["phase"] = "identity_disabling"
                            atomic_json(journal_path, journal)
                            user_info = [
                                "admin",
                                "user",
                                "info",
                                "scoped",
                                scope["access_key"],
                            ]
                            if (
                                json.loads(_cleanup_mc(scope, user_info)).get(
                                    "userStatus"
                                )
                                != "enabled"
                            ):
                                raise RuntimeError(
                                    "Image-import identity was not enabled"
                                )
                            _cleanup_mc(
                                scope,
                                [
                                    "admin",
                                    "user",
                                    "disable",
                                    "scoped",
                                    scope["access_key"],
                                ],
                            )
                            if (
                                json.loads(_cleanup_mc(scope, user_info)).get(
                                    "userStatus"
                                )
                                != "disabled"
                            ):
                                raise RuntimeError(
                                    "Image-import identity fence is unproven"
                                )
                            journal["phase"] = "identity_disabled"
                            atomic_json(journal_path, journal)
                            evidence["cleanup"]["legacy_storage_partial_probe"] = (
                                _probe_image_import_partial_retirement(
                                    backend, prepared, scope, proof
                                )
                            )
                            asyncio.run(
                                _retire_image_import_bucket(
                                    backend, prepared, scope, proof
                                )
                            )
                            if (
                                read_state(journal_path) != journal
                                or journal.get("phase") != "bucket_retired"
                                or backend.bucket_info(journal["bucket"]) is not None
                            ):
                                raise RuntimeError(
                                    "Image-import bucket retirement changed"
                                )
                            backend._accept_legacy_original_retirement(
                                prepared, journal, proof
                            )
                            evidence["cleanup"]["legacy_storage_retirement"] = {
                                "bucket_retired": True,
                                "versions_retired": len(journal["retired"]),
                                "ledger_live_through_retirement": True,
                            }
                            backend._destroy_prepared_database(prepared)
                            backend._destroy_prepared_services(prepared)
                            backend._destroy_prepared_buckets(prepared)
                            backend._finish_prepared_destruction(prepared)
                            journal["phase"] = "runtime_retired"
                            atomic_json(journal_path, journal)
                else:
                    cleanup_backend = backend
                    if (
                        container_id
                        and "tests/test_legacy_video_fence_live.py"
                        in selected_test_paths
                        and os.environ.get("AAP_LEGACY_VIDEO_FENCE_LIVE") == "1"
                    ):
                        cleanup_backend = LocalResources(
                            backend.resources,
                            backend.settings.model_copy(
                                update={
                                    "database_url": backend.url.render_as_string(
                                        hide_password=False
                                    )
                                }
                            ),
                            code_root=ROOT,
                        )
                    cleanup_backend.destroy(backend.resources["confirmation"])
                evidence["cleanup"]["local_resources_destroyed"] = True
                local_resources_destroyed = True
            except Exception as error:
                if not collect_only and physical_retirement_profile:
                    evidence["cleanup"].setdefault(
                        "retention_reason", "image_import_retirement_unproven"
                    )
                evidence["cleanup"]["errors"].append(
                    {"resource": "local-resources", "error_type": type(error).__name__}
                )
                exit_code = 1
        for label, scope in (
            ("audit_archive_writer", audit_archive_writer_storage),
            ("audit_archive_reader", audit_archive_reader_storage),
            ("audit_archive_platform", audit_archive_platform_storage),
            ("mask_content", mask_content_storage),
            ("mask_cleanup", mask_cleanup_storage),
            ("api_bootstrap", api_bootstrap_storage),
            ("image_import", image_import_storage),
            ("image_worker", image_worker_storage),
            ("media_worker", media_worker_storage),
            ("media_control", media_control_storage),
            ("import", import_storage),
            ("import_source", import_source_storage),
        ):
            if scope is not None and local_resources_destroyed:
                try:
                    if label == "image_import":
                        from worktree_env import atomic_json

                        scope["journal"]["phase"] = "identity_removing"
                        atomic_json(scope["journal_path"], scope["journal"])
                    _destroy_cleanup_storage_scope(scope)
                    if label == "image_import":
                        scope["journal"].pop("secret_key")
                        scope["journal"]["phase"] = "complete"
                        atomic_json(scope["journal_path"], scope["journal"])
                    evidence["cleanup"][label + "_storage_identity_removed"] = True
                except Exception as error:
                    evidence["cleanup"]["errors"].append(
                        {label + "_storage_identity": type(error).__name__}
                    )
                    exit_code = 1
        if source_storage is not None and local_resources_destroyed:
            try:
                _destroy_cleanup_storage_scope(source_storage)
                evidence["cleanup"]["source_storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"source_storage_identity": type(error).__name__}
                )
                exit_code = 1
        if catalog_storage is not None and local_resources_destroyed:
            try:
                _destroy_cleanup_storage_scope(catalog_storage)
                evidence["cleanup"]["catalog_storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"catalog_storage_identity": type(error).__name__}
                )
                exit_code = 1
        if cleanup_storage is not None and local_resources_destroyed:
            try:
                _destroy_cleanup_storage_scope(cleanup_storage)
                evidence["cleanup"]["storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"storage_identity": type(error).__name__}
                )
                exit_code = 1
        for label, scope in (
            ("native_source", native_source_storage),
            ("native_connection", native_connection_storage),
        ):
            if (
                scope is not None
                and local_resources_destroyed
                and native_provider_owner is None
            ):
                try:
                    _destroy_cleanup_storage_scope(scope)
                    evidence["cleanup"][f"{label}_storage_identity_removed"] = True
                except Exception as error:
                    evidence["cleanup"]["errors"].append(
                        {f"{label}_storage_identity": type(error).__name__}
                    )
                    exit_code = 1
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
                if evidence.get("ordinary_profiles"):
                    evidence["cleanup"]["retired_ordinary_logins"] = sorted(
                        access["login"]
                        for access in evidence["ordinary_profiles"].values()
                    )
                if "saved_source_io" in evidence:
                    evidence["cleanup"]["retired_saved_source_login"] = (
                        SAVED_SOURCE_ROLE
                    )
                if "storage_migration_control" in evidence:
                    evidence["cleanup"]["retired_storage_migration_control_login"] = (
                        STORAGE_MIGRATION_CONTROL_ROLE
                    )
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {
                        "resource": "postgres-container",
                        "error_type": type(error).__name__,
                    }
                )
                exit_code = 1
        if native_provider_owner is not None:
            try:
                if exit_code != 0:
                    evidence["cleanup"]["native_provider_retired"] = False
                    evidence["cleanup"]["native_provider_retention_reason"] = (
                        "failed_native_lifecycle_runtime"
                    )
                    evidence["native_provider"][
                        "retirement_requires_separate_approval"
                    ] = True
                else:
                    retired = native_provider_owner.cleanup()
                    evidence["cleanup"]["native_provider_retired"] = retired
                    if not retired:
                        raise RuntimeError("Native provider retirement failed")
                    evidence["cleanup"]["native_source_storage_identity_removed"] = True
                    evidence["cleanup"][
                        "native_connection_storage_identity_removed"
                    ] = True
                    evidence["native_provider"]["status"] = "retired"
                evidence["native_provider"]["owner_manifest_sha256"] = sha256(
                    native_provider_owner.path.read_bytes()
                )
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"native_provider": type(error).__name__}
                )
                exit_code = 1
        if local_resources_destroyed:
            try:
                _retire_resource_root(resource_root, evidence["cleanup"])
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
        result_label = (
            "COLLECTED"
            if collect_only and exit_code == 0
            else ("PASS" if exit_code == 0 else "FAIL")
        )
        scope_label = "selected subset" if args.test_files else "default selection"
        print(
            f"{result_label}: module-data {scope_label}; "
            f"evidence={output} junit={junit}",
            flush=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
