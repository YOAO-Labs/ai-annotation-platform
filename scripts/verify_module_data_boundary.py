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
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

CONTAINER_LABEL = "org.aap.acceptance"
CONTAINER_LABEL_VALUE = "module-data-boundary"
CONTAINER_ID_LABEL = "org.aap.acceptance-id"
POSTGRES_IMAGE = "postgres:16-alpine"
INFERENCE_REGISTRY_INGRESS_ROLE = "inference_registry_ingress_client"
INFERENCE_ROUTING_ROLE = "inference_routing_client"
VIDEO_EXECUTION_ROLE = "video_execution_client"
IDENTITY_API_CONSUMER_ROLE = "identity_api_consumer"
NOTIFICATION_PREFERENCES_ROLE = "notification_preferences_client"
NOTIFICATION_APPEND_ROLE = "notification_append_client"
NOTIFICATION_INBOX_ROLE = "notification_inbox_query_client"
NOTIFICATION_DELIVERY_READ_ROLE = "notification_delivery_read_client"
JOBS_LIST_QUERY_ROLE = "jobs_list_query_client"
JOBS_LIFECYCLE_ROLE = "jobs_lifecycle_client"
JOBS_DOMAIN_QUERY_FUNCTIONS = (
    "aap_jobs_v1.lock_cross_frame_singleflight(uuid,text)",
    "aap_jobs_v1.active_cross_frame_job(uuid,text)",
    "aap_jobs_v1.cross_frame_job_page(uuid,text,integer)",
    "aap_jobs_v1.cross_frame_job_count(uuid,text)",
    "aap_jobs_v1.latest_batch_prediction_times(uuid[])",
)
JOBS_ROW_LOCK_FUNCTIONS = (
    "aap_jobs_v1.read_job_for_update(uuid)",
    "aap_jobs_v1.read_job_for_share_nowait(uuid)",
)
JOBS_LIFECYCLE_FUNCTIONS = (
    "aap_jobs_v1.read_job(uuid)",
    "aap_jobs_v1.jobs_by_execution_id(text)",
    "aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)",
    "aap_jobs_v1.apply_lifecycle(uuid,jsonb)",
    "aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)",
    "aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)",
    "aap_jobs_v1.delete_batch_predict_jobs(uuid)",
    "aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])",
)
JOBS_LIST_QUERY_FUNCTIONS = (
    "aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)",
    "aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)",
)
NOTIFICATION_DELIVERY_READ_FUNCTIONS = (
    "aap_notifications_v1.read_notification(uuid)",
    "aap_notifications_v1.find_terminal_notification(uuid,text,uuid)",
    "aap_notifications_v1.delivery_job_scopes(uuid[])",
    "aap_notifications_v1.delivery_member_pairs(jsonb)",
    "aap_notifications_v1.delivery_reviewer_pairs(jsonb)",
    "aap_notifications_v1.delivery_owner_pairs(jsonb)",
    "aap_notifications_v1.delivery_super_pairs(jsonb)",
)
NOTIFICATION_INBOX_FUNCTIONS = (
    "aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)",
    "aap_notifications_v1.inbox_total_count(uuid,text)",
    "aap_notifications_v1.inbox_unread_count(uuid,text)",
)
NOTIFICATION_INBOX_PRIVATE_HELPER = (
    "aap_notifications_v1._visible_inbox_rows(uuid,text)"
)
FUNCTION_BODY_ARTIFACTS = {
    "aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])": "0364_labeling_scale_seed_operations.sql",
    "aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)": "0364_labeling_scale_seed_operations.sql",
    "aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)": "0364_labeling_scale_seed_operations.sql",
    "aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)": "0364_labeling_scale_seed_operations.sql",
    "aap_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])": "0361_labeling_compatibility_reads.sql",
    "aap_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])": "0361_labeling_compatibility_reads.sql",
    "aap_labeling_v1.next_batch_display_id()": "0361_labeling_compatibility_reads.sql",
    "aap_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)": "0352_labeling_core_read.sql",
    "aap_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)": "0352_labeling_core_read.sql",
    "aap_inference_v1.ai_mask_pool_reference_row(uuid)": "0356_inference_pool_reference.sql",
    "aap_labeling_v1.video_control_command(jsonb)": "0350_labeling_video_control.sql",
    "aap_labeling_v1.video_segment_lifecycle_command(jsonb)": "0360_labeling_video_segment_lifecycle.sql",
    "aap_labeling_v1.annotation_transfer_command(jsonb)": "0351_labeling_annotation_transfer.sql",
    "aap_labeling_v1._annotation_transfer_exact_fields(jsonb,text[])": "0351_labeling_annotation_transfer.sql",
    "aap_labeling_v1._annotation_transfer_without_track_ids(jsonb)": "0351_labeling_annotation_transfer.sql",
    "aap_labeling_v1._annotation_transfer_without_suffix(text)": "0351_labeling_annotation_transfer.sql",
    "aap_labeling_v1._annotation_transfer_camera_bbox_valid(jsonb)": "0351_labeling_annotation_transfer.sql",
    "aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)": "0352_labeling_core_read.sql",
    "aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)": "0352_labeling_core_read.sql",
    "aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)": "0352_labeling_core_read.sql",
    "aap_labeling_v1.mask_qc_command(jsonb)": "0353_labeling_mask_quality.sql",
    "aap_labeling_v1.mask_repair_command(jsonb)": "0353_labeling_mask_quality.sql",
    "aap_labeling_v1.raster_mask_retention_query(jsonb)": "0353_labeling_mask_quality.sql",
    "aap_labeling_v1._quality_exact_keys(jsonb,text[])": "0353_labeling_mask_quality.sql",
    "aap_labeling_v1._raster_mask_reference_keys()": "0353_labeling_mask_quality.sql",
    "aap_labeling_v1.spatial_quality_command(jsonb)": "0354_labeling_spatial_quality.sql",
    "aap_labeling_v1.task_maintenance_command(jsonb)": "0355_labeling_task_maintenance.sql",
    "aap_labeling_v1.discussion_command(jsonb)": "0347_labeling_discussion.sql",
    "aap_labeling_v1.task_event_command(jsonb)": "0347_labeling_discussion.sql",
    "aap_labeling_v1.mask_edit_command(jsonb)": "0348_labeling_mask_edits.sql",
    "aap_labeling_v1.mask_revision_command(jsonb)": "0348_labeling_mask_edits.sql",
    "aap_labeling_v1.ai_mask_accept_command(jsonb)": "0348_labeling_mask_edits.sql",
    "aap_labeling_v1.scene_read_query(jsonb)": "0349_labeling_scene_read.sql",
    "aap_labeling_v1._scene_read_track_json(uuid,uuid,uuid,text,text,text,jsonb,jsonb,integer,timestamptz)": "0349_labeling_scene_read.sql",
    "aap_labeling_v1._scene_read_interval_json(uuid,uuid,integer,integer,text,integer)": "0349_labeling_scene_read.sql",
    "aap_labeling_v1._scene_read_annotation_json(uuid,uuid,uuid,text,text,text,jsonb,text,uuid,text,text,boolean,boolean,boolean,boolean,integer,timestamptz,integer)": "0349_labeling_scene_read.sql",
    "aap_labeling_v1.prediction_record_command(jsonb)": "0342_labeling_prediction_records.sql",
    "aap_labeling_v1.prediction_partition_command(jsonb)": "0357_labeling_prediction_partitions.sql",
    "aap_labeling_v1.create_uploading_task(uuid,uuid,text,text,text)": "0359_labeling_upload_tasks.sql",
    "aap_labeling_v1.complete_uploading_task(uuid,uuid,uuid,text,text)": "0359_labeling_upload_tasks.sql",
    "aap_labeling_v1.scene_track_command(jsonb)": "0343_labeling_scene_track_commands.sql",
    "aap_labeling_v1.cross_frame_write_command(jsonb)": "0344_labeling_cross_frame_writes.sql",
    "aap_labeling_v1.annotation_prediction_command(jsonb)": "0345_labeling_annotation_prediction.sql",
    "aap_labeling_v1.annotation_maintenance_command(jsonb)": "0346_labeling_annotation_maintenance.sql",
    "aap_labeling_v1.annotation_bulk_command(jsonb)": "0339_labeling_annotation_bulk.sql",
    "aap_labeling_v1.video_bbox_editing_command(jsonb)": "0340_labeling_video_bbox_editing.sql",
    "aap_labeling_v1.batch_control_command(jsonb)": "0341_labeling_batch_control.sql",
    "aap_labeling_v1.annotation_statistics_command(jsonb)": "0336_labeling_annotation_statistics.sql",
    "aap_labeling_v1.scene_track_domain_command(jsonb)": "0337_labeling_scene_track_domain.sql",
    "aap_labeling_v1.annotation_write_command(jsonb)": "0338_labeling_annotation_writes.sql",
    "public.capture_mask_annotation_revision()": "0338_labeling_annotation_revision_support.sql",
    "public.record_mask_annotation_revision(uuid,uuid,uuid,integer,jsonb,text)": "0338_labeling_annotation_revision_support.sql",
    "aap_labeling_v1.task_edit_lock_command(jsonb)": "0334_labeling_task_locks.sql",
    "aap_labeling_v1.task_workflow_command(jsonb)": "0335_labeling_task_workflow.sql",
    "aap_jobs_v1.lock_action_idempotency(text,uuid,uuid,text)": "0332_jobs_action_idempotency.sql",
    "aap_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text)": "0332_jobs_action_idempotency.sql",
    "aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid)": "0331_jobs_filter_fixtures.sql",
    "aap_jobs_v1.delete_fixture_project_jobs(uuid[])": "0330_jobs_fixture_cleanup.sql",
    "aap_labeling_v1.delete_fixture_project_rows(uuid[])": "0358_labeling_fixture_cleanup.sql",
    **{
        signature: "0329_jobs_domain_queries.sql"
        for signature in JOBS_DOMAIN_QUERY_FUNCTIONS
    },
    **{signature: "0328_jobs_row_locks.sql" for signature in JOBS_ROW_LOCK_FUNCTIONS},
    **{
        signature: "0327_jobs_lifecycle.sql"
        for signature in (
            *JOBS_LIFECYCLE_FUNCTIONS,
            *(
                "aap_jobs_v1._jobs_lifecycle_has_keys(jsonb,text[])",
                "aap_jobs_v1._jobs_lifecycle_is_truthy(jsonb)",
                "aap_jobs_v1._jobs_lifecycle_integer(jsonb)",
            ),
        )
    },
    **{
        signature: "0326_jobs_list_query.sql"
        for signature in (
            *JOBS_LIST_QUERY_FUNCTIONS,
            "aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)",
        )
    },
    **{
        signature: "0325_notifications_delivery_read.sql"
        for signature in NOTIFICATION_DELIVERY_READ_FUNCTIONS
    },
    "aap_audit_v1.refresh_audit_bi_mv()": "0322_audit_daily_projection.sql",
    "aap_notifications_v1.append_job_terminal_notification(jsonb)": "0321_notifications_terminal_append.sql",
    "aap_audit_v1.query_events(pg_catalog.jsonb)": "0317_audit_query.sql",
    "aap_notifications_v1.delete_command(pg_catalog.jsonb)": "0318_notifications_delete.sql",
    **{
        signature: "0324_notifications_inbox_query.sql"
        for signature in (
            *NOTIFICATION_INBOX_FUNCTIONS,
            NOTIFICATION_INBOX_PRIVATE_HELPER,
        )
    },
}
AUDIT_APPEND_ROLE = "audit_append_client"
AUDIT_QUERY_ROLE = "audit_query_client"
NOTIFICATION_READ_ROLE = "notification_read_client"
AUDIT_READ_ROLE = "audit_read_client"
AUDIT_DAILY_REFRESH_ROLE = "audit_daily_refresh_client"
JOBS_NOTIFICATION_SCOPE_ROLE = "jobs_notification_scope_client"
READ_RELATION_SPECS = {
    "aap_labeling_v1.scene_track_rows": {
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
    "aap_labeling_v1.scene_track_interval_rows": {
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
    "aap_labeling_v1.video_segment_rows": {
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
    "aap_labeling_v1.video_tracker_job_rows": {
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
    "aap_labeling_v1.task_rows": {
        "sql": "0352_labeling_core_read.sql",
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
        ],
    },
    "aap_labeling_v1.batch_rows": {
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
    "aap_labeling_v1.annotation_rows": {
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
    "aap_labeling_v1.prediction_rows": {
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
    "aap_labeling_v1.prediction_meta_rows": {
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
    "aap_labeling_v1.failed_prediction_rows": {
        "sql": "0352_labeling_core_read.sql",
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
        ],
    },
    "aap_labeling_v1.task_lock_rows": {
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
    "aap_labeling_v1.task_dataset_item_link_rows": {
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
    "aap_labeling_v1.annotation_draft_rows": {
        "sql": "0361_labeling_compatibility_reads.sql",
        "owner": "labeling_code",
        "columns": [
            ("id", "uuid"),
            ("task_id", "uuid"),
        ],
    },
    "aap_assets_v1.labeling_dataset_item_facts": {
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
    "aap_assets_v1.scale_seed_dataset_item_facts": {
        "sql": "0364_labeling_scale_seed_operations.sql",
        "owner": "assets_code",
        "columns": [
            ("id", "uuid"),
            ("dataset_id", "uuid"),
            ("file_name", "character varying(500)"),
        ],
    },
    "aap_assets_v1.video_chapter_facts": {
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
    "aap_notifications_v1.notification_facts": {
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
    "aap_audit_v1.audit_events": {
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
    "aap_audit_v1.audit_daily_facts": {
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
    "aap_jobs_v1.notification_scope_facts": {
        "sql": "0323_jobs_notification_scope_facts.sql",
        "owner": "jobs_code",
        "columns": [
            ("id", "uuid"),
            ("kind", "character varying(40)"),
            ("scope_project_id", "uuid"),
        ],
    },
    "aap_labeling_v1.annotation_comment_facts": {
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
    "aap_labeling_v1.annotation_feedback_facts": {
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
    "aap_labeling_v1.task_event_facts": {
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
    "INFERENCE_POOL_REFERENCE": "inference_pool_reference_client",
    "LABELING_VIDEO_CONTROL": "labeling_video_control_client",
    "LABELING_ANNOTATION_TRANSFER": "labeling_annotation_transfer_client",
    "LABELING_CORE_READ": "labeling_core_read_client",
    "LABELING_MASK_QUALITY": "labeling_mask_quality_client",
    "LABELING_SPATIAL_QUALITY": "labeling_spatial_quality_client",
    "LABELING_TASK_MAINTENANCE": "labeling_task_maintenance_client",
    "AUTH": "identity_auth_client",
    "ACTOR_STATUS": "identity_actor_status_client",
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
    "PROVISIONING": "identity_provisioning_client",
    "WORKSPACE_MEMBERSHIP": "workspace_membership_client",
    "WORKSPACE_OWNER_TRANSFER": "workspace_owner_transfer_client",
    "WORKSPACE_PROJECT_MAINTENANCE": "workspace_project_maintenance_client",
    "WORKSPACE_PROJECT_DELETION": "workspace_project_deletion_client",
    "WORKSPACE_DATASET_UNLINK": "workspace_dataset_unlink_client",
    "WORKSPACE_CONFIGURATION": "workspace_configuration_client",
    "WORKSPACE_TEMPLATE_QUERY": "workspace_template_query_client",
    "WORKSPACE_PROJECT_QUERY": "workspace_project_query_client",
    "WORKSPACE_DIRECTORY_QUERY": "workspace_directory_query_client",
    "LABELING_CLASS_QUERY": "labeling_class_query_client",
    "LABELING_VIDEO_WRITER": "labeling_video_writer_client",
    "LABELING_TASK_CONTROL": "labeling_task_control_client",
    "LABELING_ANNOTATION_WRITE": "labeling_annotation_write_client",
    "LABELING_EDITING": "labeling_editing_client",
    "LABELING_BATCH_CONTROL": "labeling_batch_control_client",
    "LABELING_PREDICTION": "labeling_prediction_client",
    "LABELING_SCENE_TRACK_COMMAND": "labeling_scene_track_command_client",
    "LABELING_CROSS_FRAME": "labeling_cross_frame_client",
    "LABELING_ANNOTATION_MAINTENANCE": "labeling_annotation_maintenance_client",
    "LABELING_DISCUSSION": "labeling_discussion_client",
    "LABELING_MASK_EDIT": "labeling_mask_edit_client",
    "LABELING_SCENE_READ": "labeling_scene_read_client",
    "VIDEO_RESTORE": "video_restore_operator_client",
    "INFERENCE_PIPELINES": "inference_pipelines_client",
    "INFERENCE_BACKEND_PROBE": "inference_backend_probe_client",
    "INFERENCE_HEALTH_OBSERVATION": "inference_health_observation_client",
    "INFERENCE_HEALTH_ORIGIN": "inference_health_origin_client",
    "INFERENCE_HEALTH_DATA": "inference_health_data_client",
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
    INFERENCE_REGISTRY_INGRESS_ROLE,
    INFERENCE_ROUTING_ROLE,
    "asset_catalog_client",
    "asset_source_client",
    "asset_import_client",
    "asset_operator_client",
    "asset_media_control_client",
    "asset_media_worker_client",
    "asset_image_worker_client",
    "labeling_mask_gc_client",
    "preannotation_result_client",
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
    JOBS_NOTIFICATION_SCOPE_ROLE,
    JOBS_LIST_QUERY_ROLE,
    JOBS_LIFECYCLE_ROLE,
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
    "0364_labeling_scale_seed_operations.sql",
)
SCHEMA_ARTIFACTS = (
    "0364_labeling_scale_seed_operations.schema.json",
    "0360_labeling_video_segment_lifecycle.schema.json",
    "0359_labeling_upload_tasks.schema.json",
    "0358_labeling_fixture_cleanup.schema.json",
    "0357_labeling_prediction_partitions.schema.json",
    "0356_inference_pool_reference.schema.json",
    "0350_labeling_video_control.schema.json",
    "0351_labeling_annotation_transfer.schema.json",
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
    "0329_jobs_domain_queries.schema.json",
    "0330_jobs_fixture_cleanup.schema.json",
    "0331_jobs_filter_fixtures.schema.json",
    "0332_jobs_action_idempotency.schema.json",
    "0334_labeling_task_locks.schema.json",
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
)
TEST_FILES = (
    "tests/test_video_frame_service.py",
    "tests/test_video_control_worker_binding.py",
    "tests/test_jobs_fixture_cleanup_binding.py",
    "tests/test_legacy_files_upload.py",
    "tests/test_inference_pool_reference_native.py",
    "tests/test_labeling_video_control_native.py",
    "tests/test_labeling_annotation_transfer_native.py",
    "tests/test_labeling_core_read_native.py",
    "tests/test_labeling_mask_quality_native.py",
    "tests/test_labeling_spatial_quality_native.py",
    "tests/test_labeling_task_maintenance_native.py",
    "tests/test_labeling_scale_seed_operations.py",
    "tests/test_scale_seed_workspace_native.py",
    "tests/test_workspace_e2e_native.py",
    "tests/test_gpu_registry_delete_noop_native.py",
    "tests/test_identity_e2e_replay.py",
    "tests/test_identity_e2e_native.py",
    "tests/test_identity_e2e_live.py",
    "tests/test_identity_fixture_preferences_native.py",
    "tests/test_identity_recording_native.py",
    "tests/test_identity_recording_live.py",
    "tests/test_identity_role_audit_native.py",
    "tests/test_identity_role_audit_live.py",
    "tests/test_identity_seed_selection_live.py",
    "tests/test_identity_seed_selection_native.py",
    "tests/test_media_seed_identity_live.py",
    "tests/test_media_seed_identity_native.py",
    "tests/test_seed_parent_operator_live.py",
    "tests/test_seed_parent_operator_native.py",
    "tests/test_workspace_seed_operator_live.py",
    "tests/test_workspace_seed_operator_native.py",
    "tests/test_workspace_capacity_live.py",
    "tests/test_workspace_import_cli_live.py",
    "tests/test_workspace_worker_assembly_live.py",
    "tests/test_workspace_cross_frame_worker_live.py",
    "tests/test_workspace_video_tracker_worker_live.py",
    "tests/test_workspace_export_worker_live.py",
    "tests/test_workspace_module_assembly_live.py",
    "tests/test_workspace_module_healthcheck.py",
    "tests/test_workspace_consumer_profile_native.py",
    "tests/test_video_deletion_retirement_native.py",
    "tests/test_video_tracker_input_retention_roles.py",
    "tests/test_video_deletion_retirement_bindings.py",
    "tests/test_video_deletion_retirement_http_native.py",
    "tests/test_workspace_dataset_unlink_native.py",
    "tests/test_labeling_counter_deltas_legacy.py",
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
    "tests/test_labeling_video_writer_containers.py",
    "tests/test_labeling_video_writer_restore.py",
    "tests/test_labeling_video_writer_capacity.py",
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
    "tests/test_inference_registry_ingress_binding.py",
    "tests/test_inference_registry_ingress_postgres.py",
    "tests/test_inference_registry_ingress_selection.py",
    "tests/test_inference_registry_ingress_bindings.py",
    "tests/test_inference_registry_ingress_uow.py",
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
    "tests/test_dataset_source_auxiliary_references.py",
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


def _create_api_bootstrap_storage_scope(
    scope: dict, *, e2e_fixtures: bool = False
) -> None:
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


def _installed_function_body_sha256(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    database: str,
    owner_password: str,
    signature: str,
    sql_name: str,
) -> str:
    source = (ROOT / "apps/api/alembic/sql" / sql_name).read_text(encoding="utf-8")
    function_name = re.escape(signature.split("(", 1)[0])
    match = re.search(
        rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+{function_name}\s*\("
        r".*?\bAS\s+\$function\$(.*?)\$function\$;",
        source,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError(f"frozen SQL has no function body: {sql_name}")
    expected = match.group(1)
    encoded = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=(
            "SELECT pg_catalog.encode(pg_catalog.convert_to(prosrc, 'UTF8'), 'hex') "
            "FROM pg_catalog.pg_proc WHERE oid = "
            f"pg_catalog.to_regprocedure('{signature}')"
        ),
    ).strip()
    try:
        actual = bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeError):
        raise RuntimeError(
            f"installed function body is missing or malformed: {signature}"
        ) from None
    if actual != expected:
        raise RuntimeError(
            f"installed function body differs from frozen SQL: {signature}"
        )
    return hashlib.sha256(actual.encode("utf-8")).hexdigest()


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
        f'GRANT USAGE ON SCHEMA aap_audit_v1 TO "{AUDIT_DAILY_REFRESH_ROLE}"; '
        f'GRANT EXECUTE ON FUNCTION aap_audit_v1.refresh_audit_bi_mv() TO "{AUDIT_DAILY_REFRESH_ROLE}";',
    )

    async def verify_refresh_login() -> None:
        import asyncpg

        from aap_owner_data.postgres import verify_function_only_role

        connection = await asyncpg.connect(refresh_url, timeout=5)
        try:
            await verify_function_only_role(
                connection,
                schema="aap_audit_v1",
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
            "'owner_audit_create',has_schema_privilege('audit_code','aap_audit_v1','CREATE'),"
            "'refresh_temp',has_database_privilege("
            f"'{AUDIT_DAILY_REFRESH_ROLE}',current_database(),'TEMP')) "
            "FROM pg_class AS mv,pg_class AS facts,pg_proc AS refresh "
            "WHERE mv.oid='public.mv_audit_bi_daily'::regclass "
            "AND facts.oid='aap_audit_v1.audit_daily_facts'::regclass "
            "AND refresh.oid='aap_audit_v1.refresh_audit_bi_mv()'::regprocedure;",
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
        "BEGIN; SELECT aap_audit_v1.refresh_audit_bi_mv(); COMMIT;",
    )
    projected_count = execute(
        AUDIT_READ_ROLE,
        read_password,
        "SELECT event_count::text FROM aap_audit_v1.audit_daily_facts "
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
                "SELECT aap_audit_v1.refresh_audit_bi_mv()",
            ),
        ),
        (
            AUDIT_READ_ROLE,
            read_password,
            (
                "SELECT * FROM public.mv_audit_bi_daily LIMIT 1",
                "SELECT aap_audit_v1.refresh_audit_bi_mv()",
            ),
        ),
        (
            AUDIT_DAILY_REFRESH_ROLE,
            refresh_password,
            (
                "SELECT * FROM public.audit_logs LIMIT 1",
                "SELECT * FROM public.mv_audit_bi_daily LIMIT 1",
                "SELECT * FROM aap_audit_v1.audit_daily_facts LIMIT 1",
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
                        "SELECT pg_catalog.set_config($1,$2,true)", f"aap.{key}", value
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


def _read_relation_evidence_expression() -> str:
    targets = ", ".join(f"('{relation_name}')" for relation_name in READ_RELATION_SPECS)
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
    value: str, *, expected_owner: str | dict[str, str] | None = None
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
    jobs_notification_scope_password: str,
    jobs_list_query_password: str,
    jobs_lifecycle_password: str,
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
            "CREATE ROLE notification_preferences_client LOGIN "
            f"PASSWORD '{preference_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE notification_append_client LOGIN "
            f"PASSWORD '{notification_append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {NOTIFICATION_INBOX_ROLE} LOGIN "
            f"PASSWORD '{notification_inbox_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {NOTIFICATION_DELIVERY_READ_ROLE} LOGIN "
            f"PASSWORD '{notification_delivery_read_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE audit_append_client LOGIN "
            f"PASSWORD '{audit_append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE audit_query_client LOGIN "
            f"PASSWORD '{audit_query_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE notification_read_client LOGIN "
            f"PASSWORD '{notification_read_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE audit_read_client LOGIN "
            f"PASSWORD '{audit_read_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f"CREATE ROLE {AUDIT_DAILY_REFRESH_ROLE} LOGIN "
            f"PASSWORD '{audit_daily_refresh_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
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
            'REVOKE TEMPORARY ON DATABASE :"expected_database" FROM PUBLIC;\n'
            'GRANT CONNECT ON DATABASE :"expected_database" TO '
            f'"{NOTIFICATION_READ_ROLE}", "{AUDIT_READ_ROLE}", '
            f'"{AUDIT_DAILY_REFRESH_ROLE}", "{JOBS_NOTIFICATION_SCOPE_ROLE}";\n'
            'GRANT TEMPORARY ON DATABASE :"expected_database" TO '
            + ", ".join(
                ("legacy_runtime",)
                + tuple(
                    role
                    for role in MODULE_ROLES
                    if role
                    not in {
                        "audit_code",
                        "asset_operator_client",
                        "asset_media_control_client",
                        "asset_media_worker_client",
                        "asset_image_worker_client",
                        "labeling_mask_gc_client",
                        "preannotation_result_client",
                        NOTIFICATION_PREFERENCES_ROLE,
                        NOTIFICATION_APPEND_ROLE,
                        NOTIFICATION_INBOX_ROLE,
                        NOTIFICATION_DELIVERY_READ_ROLE,
                        AUDIT_APPEND_ROLE,
                        AUDIT_QUERY_ROLE,
                        NOTIFICATION_READ_ROLE,
                        AUDIT_READ_ROLE,
                        AUDIT_DAILY_REFRESH_ROLE,
                        JOBS_NOTIFICATION_SCOPE_ROLE,
                        JOBS_LIST_QUERY_ROLE,
                        JOBS_LIFECYCLE_ROLE,
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
        "SELECT aap_identity_briefs_v1.briefs_policy();\n"
        "SELECT aap_identity_briefs_v1.execute_briefs_command('{}'::jsonb);\n"
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
        "SELECT aap_insights_v1.clear_project_task_views(:'project_id'::uuid);\n"
        "SELECT aap_diagnostics_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_export_v1.clear_project_export_artifacts(:'project_id'::uuid);\n"
        "SELECT aap_jobs_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_learning_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.append_dataset_items("
        ":'dataset_id'::uuid, ARRAY[]::uuid[], :'operation_id'::uuid);\n"
        "SELECT 'AAP_READ_RELATIONS_FROZEN=' || ("
        f"{_read_relation_evidence_expression()}"
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
        relation_rows[0], expected_owner=migration_role
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
            "AND to_regnamespace('aap_workspace_owner_transfer_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_template_query_v1') IS NULL "
            "AND to_regnamespace('aap_workspace_project_query_v1') IS NULL "
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
        "identity_api_consumer": None,
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
        "test_exit_code": None,
        "migrations": {
            "head_before_downgrade": None,
            "installed_function_body_sha256": {},
            "read_relation_hash_method": {
                "view_definition": "SHA-256 of UTF-8 pg_catalog.pg_get_viewdef(oid, true) output",
                "migration_sql": "SHA-256 of exact migration SQL file bytes; not compared to the deparser digest",
            },
            "read_relation_sql_input_sha256": {},
            "read_relations_before_downgrade": None,
            "read_relations_frozen_sql_transaction": None,
            "read_relations_after_upgrade": None,
            "read_relations_after_owner_grants": None,
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
        node_bin = shutil.which("node")
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
        catalog_grants_path = ROOT / "infra/docker/assets-catalog-grants.sql"
        source_grants_path = ROOT / "infra/docker/assets-source-grants.sql"
        import_grants_path = ROOT / "infra/docker/assets-import-grants.sql"
        operator_grants_path = ROOT / "infra/docker/assets-operator-grants.sql"
        media_grants_path = ROOT / "infra/docker/assets-media-grants.sql"
        image_grants_path = ROOT / "infra/docker/assets-image-grants.sql"
        mask_gc_grants_path = ROOT / "infra/docker/labeling-mask-gc-data-grants.sql"
        result_grants_path = ROOT / "infra/docker/preannotation-result-data-grants.sql"
        identity_grants_path = ROOT / "infra/docker/identity-auth-grants.sql"
        identity_actor_status_grants_path = (
            ROOT / "infra/docker/identity-actor-status-grants.sql"
        )
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
        identity_provisioning_grants_path = (
            ROOT / "infra/docker/identity-provisioning-grants.sql"
        )
        workspace_membership_grants_path = (
            ROOT / "infra/docker/workspace-membership-grants.sql"
        )
        workspace_owner_transfer_grants_path = (
            ROOT / "infra/docker/workspace-owner-transfer-grants.sql"
        )
        workspace_project_maintenance_grants_path = (
            ROOT / "infra/docker/workspace-project-maintenance-grants.sql"
        )
        workspace_project_deletion_grants_path = (
            ROOT / "infra/docker/workspace-project-deletion-grants.sql"
        )
        workspace_dataset_unlink_grants_path = (
            ROOT / "infra/docker/workspace-dataset-unlink-grants.sql"
        )
        workspace_configuration_grants_path = (
            ROOT / "infra/docker/workspace-configuration-grants.sql"
        )
        workspace_template_query_grants_path = (
            ROOT / "infra/docker/workspace-template-query-grants.sql"
        )
        workspace_project_query_grants_path = (
            ROOT / "infra/docker/workspace-project-query-grants.sql"
        )
        workspace_directory_query_grants_path = (
            ROOT / "infra/docker/workspace-directory-query-grants.sql"
        )
        labeling_class_query_grants_path = (
            ROOT / "infra/docker/labeling-class-query-grants.sql"
        )
        labeling_video_writer_grants_path = (
            ROOT / "infra/docker/labeling-video-writer-grants.sql"
        )
        labeling_task_control_grants_path = (
            ROOT / "infra/docker/labeling-task-control-grants.sql"
        )
        labeling_annotation_writes_grants_path = (
            ROOT / "infra/docker/labeling-annotation-writes-grants.sql"
        )
        labeling_editing_grants_path = ROOT / "infra/docker/labeling-editing-grants.sql"
        labeling_batch_control_grants_path = (
            ROOT / "infra/docker/labeling-batch-control-grants.sql"
        )
        labeling_prediction_grants_path = (
            ROOT / "infra/docker/labeling-prediction-grants.sql"
        )
        labeling_scene_track_commands_grants_path = (
            ROOT / "infra/docker/labeling-scene-track-commands-grants.sql"
        )
        labeling_cross_frame_grants_path = (
            ROOT / "infra/docker/labeling-cross-frame-grants.sql"
        )
        labeling_annotation_maintenance_grants_path = (
            ROOT / "infra/docker/labeling-annotation-maintenance-grants.sql"
        )
        labeling_discussion_grants_path = (
            ROOT / "infra/docker/labeling-discussion-grants.sql"
        )
        labeling_mask_edit_grants_path = (
            ROOT / "infra/docker/labeling-mask-edit-grants.sql"
        )
        labeling_scene_read_grants_path = (
            ROOT / "infra/docker/labeling-scene-read-grants.sql"
        )
        labeling_video_control_grants_path = (
            ROOT / "infra/docker/labeling-video-control-grants.sql"
        )
        labeling_annotation_transfer_grants_path = (
            ROOT / "infra/docker/labeling-annotation-transfer-grants.sql"
        )
        labeling_core_read_grants_path = (
            ROOT / "infra/docker/labeling-core-read-grants.sql"
        )
        labeling_mask_quality_grants_path = (
            ROOT / "infra/docker/labeling-mask-quality-grants.sql"
        )
        labeling_spatial_quality_grants_path = (
            ROOT / "infra/docker/labeling-spatial-quality-grants.sql"
        )
        labeling_task_maintenance_grants_path = (
            ROOT / "infra/docker/labeling-task-maintenance-grants.sql"
        )
        inference_pool_reference_grants_path = (
            ROOT / "infra/docker/inference-pool-reference-grants.sql"
        )
        video_restore_grants_path = ROOT / "infra/docker/video-restore-grants.sql"
        inference_pipelines_grants_path = (
            ROOT / "infra/docker/inference-pipelines-grants.sql"
        )
        inference_backend_probe_grants_path = (
            ROOT / "infra/docker/inference-backend-probe-grants.sql"
        )
        inference_health_observation_grants_path = (
            ROOT / "infra/docker/inference-health-observation-grants.sql"
        )
        inference_health_origin_grants_path = (
            ROOT / "infra/docker/inference-health-origin-grants.sql"
        )
        inference_health_data_grants_path = (
            ROOT / "infra/docker/inference-health-data-grants.sql"
        )
        inference_registry_ingress_grants_path = (
            ROOT / "infra/docker/inference-registry-ingress-grants.sql"
        )
        inference_routing_grants_path = (
            ROOT / "infra/docker/inference-routing-grants.sql"
        )
        video_execution_grants_path = ROOT / "infra/docker/video-execution-grants.sql"
        identity_deactivation_grants_path = (
            ROOT / "infra/docker/identity-deactivation-grants.sql"
        )
        notification_preferences_grants_path = (
            ROOT / "infra/docker/notification-preferences-grants.sql"
        )
        notification_append_grants_path = (
            ROOT / "infra/docker/notification-append-grants.sql"
        )
        notification_inbox_grants_path = (
            ROOT / "infra/docker/notification-inbox-grants.sql"
        )
        notification_delivery_read_grants_path = (
            ROOT / "infra/docker/notification-delivery-read-grants.sql"
        )
        notification_read_grants_path = (
            ROOT / "infra/docker/notifications-read-grants.sql"
        )
        audit_append_grants_path = ROOT / "infra/docker/audit-append-grants.sql"
        audit_query_grants_path = ROOT / "infra/docker/audit-query-grants.sql"
        audit_read_grants_path = ROOT / "infra/docker/audit-read-grants.sql"
        jobs_scope_grants_path = (
            ROOT / "infra/docker/jobs-notification-scope-grants.sql"
        )
        jobs_list_query_grants_path = ROOT / "infra/docker/jobs-list-query-grants.sql"
        jobs_lifecycle_grants_path = ROOT / "infra/docker/jobs-lifecycle-grants.sql"
        identity_recovery_grants_path = (
            ROOT / "infra/docker/identity-recovery-grants.sql"
        )
        lan_production_grants_path = ROOT / "infra/docker/lan-production-grants.sql"
        identity_consumer_grants_path = (
            ROOT / "infra/docker/identity-consumer-grants.sql"
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
            or not identity_actor_status_grants_path.is_file()
            or not identity_key_grants_path.is_file()
            or not identity_session_grants_path.is_file()
            or not identity_deactivation_grants_path.is_file()
            or not notification_preferences_grants_path.is_file()
            or not notification_append_grants_path.is_file()
            or not notification_inbox_grants_path.is_file()
            or not notification_delivery_read_grants_path.is_file()
            or not notification_read_grants_path.is_file()
            or not audit_append_grants_path.is_file()
            or not audit_query_grants_path.is_file()
            or not audit_read_grants_path.is_file()
            or not jobs_scope_grants_path.is_file()
            or not jobs_list_query_grants_path.is_file()
            or not jobs_lifecycle_grants_path.is_file()
            or not identity_group_grants_path.is_file()
            or not identity_invitation_grants_path.is_file()
            or not identity_directory_grants_path.is_file()
            or not identity_admin_password_grants_path.is_file()
            or not identity_platform_role_grants_path.is_file()
            or not identity_suspension_grants_path.is_file()
            or not identity_deletion_grants_path.is_file()
            or not identity_offboarding_grants_path.is_file()
            or not identity_briefs_grants_path.is_file()
            or not identity_provisioning_grants_path.is_file()
            or not workspace_membership_grants_path.is_file()
            or not workspace_owner_transfer_grants_path.is_file()
            or not workspace_project_maintenance_grants_path.is_file()
            or not workspace_project_deletion_grants_path.is_file()
            or not workspace_dataset_unlink_grants_path.is_file()
            or not workspace_configuration_grants_path.is_file()
            or not workspace_template_query_grants_path.is_file()
            or not workspace_project_query_grants_path.is_file()
            or not workspace_directory_query_grants_path.is_file()
            or not labeling_class_query_grants_path.is_file()
            or not labeling_video_writer_grants_path.is_file()
            or not labeling_task_control_grants_path.is_file()
            or not labeling_annotation_writes_grants_path.is_file()
            or not labeling_editing_grants_path.is_file()
            or not labeling_batch_control_grants_path.is_file()
            or not labeling_prediction_grants_path.is_file()
            or not labeling_scene_track_commands_grants_path.is_file()
            or not labeling_cross_frame_grants_path.is_file()
            or not labeling_annotation_maintenance_grants_path.is_file()
            or not labeling_discussion_grants_path.is_file()
            or not labeling_mask_edit_grants_path.is_file()
            or not labeling_scene_read_grants_path.is_file()
            or not labeling_video_control_grants_path.is_file()
            or not labeling_annotation_transfer_grants_path.is_file()
            or not labeling_core_read_grants_path.is_file()
            or not labeling_mask_quality_grants_path.is_file()
            or not labeling_spatial_quality_grants_path.is_file()
            or not labeling_task_maintenance_grants_path.is_file()
            or not video_restore_grants_path.is_file()
            or not inference_pipelines_grants_path.is_file()
            or not inference_backend_probe_grants_path.is_file()
            or not inference_health_observation_grants_path.is_file()
            or not inference_health_origin_grants_path.is_file()
            or not inference_health_data_grants_path.is_file()
            or not inference_pool_reference_grants_path.is_file()
            or not inference_registry_ingress_grants_path.is_file()
            or not inference_routing_grants_path.is_file()
            or not video_execution_grants_path.is_file()
            or not identity_account_grants_path.is_file()
            or not identity_recovery_grants_path.is_file()
            or not lan_production_grants_path.is_file()
            or not identity_consumer_grants_path.is_file()
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
                or not isinstance(contract.get("$defs", {}), dict)
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
        identity_actor_status_grant_sql = identity_actor_status_grants_path.read_text(
            encoding="utf-8"
        )
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
        identity_provisioning_grant_sql = identity_provisioning_grants_path.read_text(
            encoding="utf-8"
        )
        workspace_membership_grant_sql = workspace_membership_grants_path.read_text(
            encoding="utf-8"
        )
        workspace_owner_transfer_grant_sql = (
            workspace_owner_transfer_grants_path.read_text(encoding="utf-8")
        )
        workspace_project_maintenance_grant_sql = (
            workspace_project_maintenance_grants_path.read_text(encoding="utf-8")
        )
        workspace_project_deletion_grant_sql = (
            workspace_project_deletion_grants_path.read_text(encoding="utf-8")
        )
        workspace_dataset_unlink_grant_sql = (
            workspace_dataset_unlink_grants_path.read_text(encoding="utf-8")
        )
        workspace_configuration_grant_sql = (
            workspace_configuration_grants_path.read_text(encoding="utf-8")
        )
        workspace_template_query_grant_sql = (
            workspace_template_query_grants_path.read_text(encoding="utf-8")
        )
        workspace_directory_query_grant_sql = (
            workspace_directory_query_grants_path.read_text(encoding="utf-8")
        )
        labeling_class_query_grant_sql = labeling_class_query_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_video_writer_grant_sql = labeling_video_writer_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_task_control_grant_sql = labeling_task_control_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_annotation_writes_grant_sql = (
            labeling_annotation_writes_grants_path.read_text(encoding="utf-8")
        )
        labeling_editing_grant_sql = labeling_editing_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_batch_control_grant_sql = labeling_batch_control_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_prediction_grant_sql = labeling_prediction_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_scene_track_commands_grant_sql = (
            labeling_scene_track_commands_grants_path.read_text(encoding="utf-8")
        )
        labeling_cross_frame_grant_sql = labeling_cross_frame_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_annotation_maintenance_grant_sql = (
            labeling_annotation_maintenance_grants_path.read_text(encoding="utf-8")
        )
        labeling_discussion_grant_sql = labeling_discussion_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_mask_edit_grant_sql = labeling_mask_edit_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_scene_read_grant_sql = labeling_scene_read_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_video_control_grant_sql = labeling_video_control_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_annotation_transfer_grant_sql = (
            labeling_annotation_transfer_grants_path.read_text(encoding="utf-8")
        )
        labeling_core_read_grant_sql = labeling_core_read_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_mask_quality_grant_sql = labeling_mask_quality_grants_path.read_text(
            encoding="utf-8"
        )
        labeling_spatial_quality_grant_sql = (
            labeling_spatial_quality_grants_path.read_text(encoding="utf-8")
        )
        labeling_task_maintenance_grant_sql = (
            labeling_task_maintenance_grants_path.read_text(encoding="utf-8")
        )
        inference_pool_reference_grant_sql = (
            inference_pool_reference_grants_path.read_text(encoding="utf-8")
        )
        video_restore_grant_sql = video_restore_grants_path.read_text(encoding="utf-8")
        inference_pipelines_grant_sql = inference_pipelines_grants_path.read_text(
            encoding="utf-8"
        )
        inference_backend_probe_grant_sql = (
            inference_backend_probe_grants_path.read_text(encoding="utf-8")
        )
        inference_health_observation_grant_sql = (
            inference_health_observation_grants_path.read_text(encoding="utf-8")
        )
        inference_health_origin_grant_sql = (
            inference_health_origin_grants_path.read_text(encoding="utf-8")
        )
        inference_health_data_grant_sql = inference_health_data_grants_path.read_text(
            encoding="utf-8"
        )
        inference_registry_ingress_grant_sql = (
            inference_registry_ingress_grants_path.read_text(encoding="utf-8")
        )
        inference_routing_grant_sql = inference_routing_grants_path.read_text(
            encoding="utf-8"
        )
        video_execution_grant_sql = video_execution_grants_path.read_text(
            encoding="utf-8"
        )
        workspace_project_query_grant_sql = (
            workspace_project_query_grants_path.read_text(encoding="utf-8")
        )
        identity_group_grant_sql = identity_group_grants_path.read_text(
            encoding="utf-8"
        )
        identity_deactivation_grant_sql = identity_deactivation_grants_path.read_text(
            encoding="utf-8"
        )
        notification_preferences_grant_sql = (
            notification_preferences_grants_path.read_text(encoding="utf-8")
        )
        notification_append_grant_sql = notification_append_grants_path.read_text(
            encoding="utf-8"
        )
        notification_inbox_grant_sql = notification_inbox_grants_path.read_text(
            encoding="utf-8"
        )
        notification_delivery_read_grant_sql = (
            notification_delivery_read_grants_path.read_text(encoding="utf-8")
        )
        notification_read_grant_sql = notification_read_grants_path.read_text(
            encoding="utf-8"
        )
        audit_append_grant_sql = audit_append_grants_path.read_text(encoding="utf-8")
        audit_query_grant_sql = audit_query_grants_path.read_text(encoding="utf-8")
        audit_read_grant_sql = audit_read_grants_path.read_text(encoding="utf-8")
        jobs_scope_grant_sql = jobs_scope_grants_path.read_text(encoding="utf-8")
        identity_account_grant_sql = identity_account_grants_path.read_text(
            encoding="utf-8"
        )
        identity_recovery_grant_sql = identity_recovery_grants_path.read_text(
            encoding="utf-8"
        )
        lan_production_grant_sql = lan_production_grants_path.read_text(
            encoding="utf-8"
        )
        identity_consumer_grant_sql = identity_consumer_grants_path.read_text(
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
        jobs_notification_scope_password = f"jobs-scope-{secrets.token_hex(16)}"
        jobs_list_query_password = f"jobs-list-{secrets.token_hex(16)}"
        jobs_lifecycle_password = f"jobs-lifecycle-{secrets.token_hex(16)}"
        inference_registry_ingress_password = secrets.token_hex(24)
        inference_routing_password = secrets.token_hex(24)
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
        if heads != ["0361"]:
            raise RuntimeError("module-data acceptance requires migration head 0361")
        evidence["migrations"]["head_before_downgrade"] = heads[0]
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
        evidence["migrations"]["read_relations_before_downgrade"] = (
            read_relations_before
        )
        installed_function_bodies = {
            signature: _installed_function_body_sha256(
                container_id,
                container_name,
                container_identity,
                database=database,
                owner_password=owner_password,
                signature=signature,
                sql_name=sql_name,
            )
            for signature, sql_name in FUNCTION_BODY_ARTIFACTS.items()
        }
        evidence["migrations"]["installed_function_body_sha256"] = (
            installed_function_bodies
        )
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
        read_relations_frozen = _install_and_rollback_frozen_sql(
            container_id,
            container_name,
            container_identity,
            database,
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
        phase = "upgrade-to-head"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _run_alembic(backend, "upgrade", "head")
        if set(asyncio.run(backend.revisions())) != set(heads):
            raise RuntimeError("disposable database did not return to migration head")
        evidence["migrations"]["upgraded_to_head"] = True
        evidence["migrations"]["head_after_upgrade"] = heads[0]
        read_relations_after = _installed_read_relation_evidence(
            container_id,
            container_name,
            container_identity,
            database=database,
            owner_password=owner_password,
            expected_owner="owner",
        )
        _assert_read_relation_structure_matches(
            read_relations_before,
            read_relations_after,
            phase="migration upgrade",
        )
        evidence["migrations"]["read_relations_after_upgrade"] = read_relations_after
        if {
            signature: _installed_function_body_sha256(
                container_id,
                container_name,
                container_identity,
                database=database,
                owner_password=owner_password,
                signature=signature,
                sql_name=sql_name,
            )
            for signature, sql_name in FUNCTION_BODY_ARTIFACTS.items()
        } != installed_function_bodies:
            raise RuntimeError(
                "installed SQL function bodies changed after migration roundtrip"
            )
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
            jobs_notification_scope_password,
            jobs_list_query_password,
            jobs_lifecycle_password,
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
                "CREATE ROLE inference_registry_ingress_client LOGIN "
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
                "CREATE ROLE inference_routing_client LOGIN "
                f"PASSWORD '{inference_routing_password}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "NOREPLICATION NOBYPASSRLS;\n"
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
                "inference_registry_ingress_role": INFERENCE_REGISTRY_INGRESS_ROLE,
                "inference_routing_role": INFERENCE_ROUTING_ROLE,
            },
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
                sql=jobs_list_query_grants_path.read_text(encoding="utf-8"),
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
                sql=jobs_lifecycle_grants_path.read_text(encoding="utf-8"),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "jobs_owner": "jobs_code",
                    "api_role": "legacy_runtime",
                    "lifecycle_client": JOBS_LIFECYCLE_ROLE,
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
        evidence["jobs_list_query"]["function_only_grants"] = True
        phase = "install-notification-delivery-read-grants"
        apply_notification_delivery_read_grants()
        evidence["notification_delivery_read"]["api_function_grant"] = True
        evidence["notification_read_relations"]["api_owner_function_grant"] = True

        async def verify_delivery_read_login() -> None:
            import asyncpg

            from aap_owner_data.postgres import verify_function_only_role

            connection = await asyncpg.connect(
                notification_delivery_read_url, timeout=5
            )
            try:
                await verify_function_only_role(
                    connection,
                    schema="aap_notifications_v1",
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

            from aap_owner_data.postgres import verify_function_only_role

            connection = await asyncpg.connect(notification_inbox_url, timeout=5)
            try:
                await verify_function_only_role(
                    connection,
                    schema="aap_notifications_v1",
                    functions={
                        ("inbox_page", "uuid, text, boolean, integer, integer"),
                        ("inbox_total_count", "uuid, text"),
                        ("inbox_unread_count", "uuid, text"),
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
                "(id, email, name, password_hash, role, is_active, status) VALUES ("
                f"'{notification_preference_user_id}'::uuid, "
                f"'notification-preference-{notification_preference_user_id}@test.invalid', "
                "'Preference owner fixture', 'module-data-fixture', 'employee', true, 'offline');"
            ),
        )
        api_get_request = json.dumps(
            {
                "schema": "aap-notification-preferences/v1",
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
                "SELECT aap_notifications_v1.preference_command("
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
            "SELECT id FROM aap_notifications_v1.notification_facts LIMIT 1"
        )
        notification_fact_write_statements = (
            "INSERT INTO aap_notifications_v1.notification_facts "
            "(id,user_id,type,target_type,target_id,payload) "
            "SELECT pg_catalog.gen_random_uuid(),pg_catalog.gen_random_uuid(),"
            "'task.approved','task',pg_catalog.gen_random_uuid(),'{}'::jsonb WHERE false",
            "UPDATE aap_notifications_v1.notification_facts "
            "SET read_at=pg_catalog.now() WHERE false",
            "DELETE FROM aap_notifications_v1.notification_facts WHERE false",
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
                "SELECT aap_notifications_v1.preference_command("
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
                "SELECT aap_notifications_v1.append_notification("
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
                "SELECT aap_notifications_v1.append_job_terminal_notification("
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
                "SELECT aap_notifications_v1.read_state_command("
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
                "SELECT aap_notifications_v1.delete_command("
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
                "SELECT aap_notifications_v1.append_notification("
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
                "SELECT count(*) FROM aap_notifications_v1.read_notification("
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
            "SELECT aap_notifications_v1.preference_command('{}'::jsonb)",
            "SELECT aap_notifications_v1.append_notification('{}'::jsonb)",
            "SELECT aap_notifications_v1.append_job_terminal_notification('{}'::jsonb)",
            "SELECT aap_notifications_v1.read_state_command('{}'::jsonb)",
            "SELECT aap_notifications_v1.delete_command('{}'::jsonb)",
            "SELECT aap_notifications_v1.identity_delivery_policy()",
            "SELECT aap_notifications_v1.claim_identity_deliveries('{}'::jsonb)",
            "SELECT aap_notifications_v1.finish_identity_delivery('{}'::jsonb)",
            "SELECT aap_notifications_v1.create_identity_deactivation_notifications("
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
                "WHERE namespace.nspname='aap_notifications_v1' "
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
                "SELECT aap_audit_v1.append_events("
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
                "SELECT aap_audit_v1.query_events("
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
                "SELECT count(*) FROM aap_audit_v1.audit_events "
                f"WHERE id='{audit_append_id}'::bigint;"
            ),
        )
        if api_audit_fact.strip() != "1":
            raise RuntimeError("API LOGIN could not read Audit event facts")
        audit_fact_write_statements = (
            "INSERT INTO aap_audit_v1.audit_events (action) "
            "SELECT 'audit.read_relation.denied' WHERE false",
            "UPDATE aap_audit_v1.audit_events SET action='audit.denied' WHERE false",
            "DELETE FROM aap_audit_v1.audit_events WHERE false",
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
                "SELECT count(*)=1 FROM aap_notifications_v1.inbox_page("
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
                    "SELECT * FROM aap_notifications_v1._visible_inbox_rows("
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
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=(
                "INSERT INTO public.async_jobs(id,kind,status,payload,result) VALUES ("
                f"'{jobs_scope_id}'::uuid,'{jobs_scope_kind}','completed','{{}}'::jsonb,'{{}}'::jsonb);"
            ),
        )
        api_jobs_fact = _psql(
            container_id,
            container_name,
            container_identity,
            username="legacy_runtime",
            password=runtime_password,
            database=database,
            sql=(
                "SELECT row_to_json(fact) FROM aap_jobs_v1.notification_scope_facts AS fact "
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

        async def seed_delivery_project() -> str:
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
                    await create_membership(
                        session,
                        project_id=project_id,
                        user_id=actor_id,
                        role="reviewer",
                        assigned_by=actor_id,
                    )
                    await session.commit()
                    return str(project_id)
            finally:
                await engine.dispose()

        delivery_project_id = asyncio.run(seed_delivery_project())
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
                "labeling_video_writer_role": "labeling_video_writer_client",
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
                "task_control_role": "labeling_task_control_client",
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
                "annotation_write_role": "labeling_annotation_write_client",
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
                "editing_role": "labeling_editing_client",
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
                "batch_control_role": "labeling_batch_control_client",
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
                "prediction_role": "labeling_prediction_client",
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
                "scene_track_command_role": "labeling_scene_track_command_client",
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
                "cross_frame_role": "labeling_cross_frame_client",
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
                "annotation_maintenance_role": "labeling_annotation_maintenance_client",
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
                "discussion_role": "labeling_discussion_client",
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
                "mask_edit_role": "labeling_mask_edit_client",
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
                "scene_read_role": "labeling_scene_read_client",
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
                "video_control_role": "labeling_video_control_client",
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
                "annotation_transfer_role": "labeling_annotation_transfer_client",
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
                "core_read_role": "labeling_core_read_client",
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
                "quality_role": "labeling_mask_quality_client",
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
                "spatial_quality_role": "labeling_spatial_quality_client",
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
                "task_maintenance_role": "labeling_task_maintenance_client",
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
                "pool_reference_role": "inference_pool_reference_client",
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
                "video_restore_operator_role": "video_restore_operator_client",
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
                "SELECT aap_notifications_v1.identity_delivery_policy() ->> 'policy';"
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
                "BEGIN; SELECT aap_notifications_v1.claim_identity_deliveries("
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
                "SELECT aap_notifications_v1.finish_identity_delivery("
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
                "identity_provisioning_role": "identity_provisioning_client",
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
                "workspace_membership_role": "workspace_membership_client",
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
                "workspace_owner_transfer_role": "workspace_owner_transfer_client",
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
                "workspace_project_maintenance_role": "workspace_project_maintenance_client",
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
                "workspace_project_deletion_role": "workspace_project_deletion_client",
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
                "workspace_dataset_unlink_role": "workspace_dataset_unlink_client",
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
                "workspace_configuration_role": "workspace_configuration_client",
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
                "inference_pipelines_role": "inference_pipelines_client",
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
                "inference_backend_probe_role": "inference_backend_probe_client",
            },
        )
        for label, grant_sql, variable, role in (
            (
                "observation",
                inference_health_observation_grant_sql,
                "inference_health_observation_role",
                "inference_health_observation_client",
            ),
            (
                "origin",
                inference_health_origin_grant_sql,
                "inference_health_origin_role",
                "inference_health_origin_client",
            ),
            (
                "data",
                inference_health_data_grant_sql,
                "inference_health_data_role",
                "inference_health_data_client",
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
                sql=grant_sql,
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
                "workspace_template_query_role": "workspace_template_query_client",
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
                "workspace_project_query_role": "workspace_project_query_client",
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
                "workspace_directory_query_role": "workspace_directory_query_client",
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
                "labeling_class_query_role": "labeling_class_query_client",
            },
        )
        resource_environment = backend.environment()
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
        phase = "install-identity-api-consumer-broad-grants"
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
                "application_role": IDENTITY_API_CONSUMER_ROLE,
                "migration_role": "owner",
                "asset_cleanup_role": "asset_cleanup_client",
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
                "SELECT count(*) FROM aap_notifications_v1.read_notification("
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
                "SELECT EXISTS(SELECT FROM aap_notifications_v1.inbox_page("
                f"'{notification_preference_user_id}'::uuid,'employee',false,100,0) "
                f"WHERE id='{read_relation_notification_id}'::uuid) "
                "AND aap_notifications_v1.inbox_total_count("
                f"'{notification_preference_user_id}'::uuid,'employee')>=1 "
                "AND aap_notifications_v1.inbox_unread_count("
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
                "SELECT count(*) FROM aap_audit_v1.audit_events "
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
        if (
            "tests/test_seed_parent_operator_native.py" in selected_test_paths
            or "tests/test_workspace_e2e_native.py" in selected_test_paths
            or "tests/test_identity_e2e_live.py" in selected_test_paths
            or "tests/test_scale_seed_workspace_native.py" in selected_test_paths
            or "tests/test_identity_seed_selection_live.py" in selected_test_paths
            or "tests/test_identity_recording_live.py" in selected_test_paths
            or "tests/test_media_seed_workspace_native.py" in selected_test_paths
            or "tests/test_media_seed_workspace_live.py" in selected_test_paths
            or "tests/test_seed_parent_operator_live.py" in selected_test_paths
            or "tests/test_workspace_seed_operator_native.py" in selected_test_paths
            or "tests/test_workspace_seed_operator_live.py" in selected_test_paths
            or (
                "tests/test_workspace_module_assembly_live.py" in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_MODULE_ASSEMBLY_LIVE") == "1"
            )
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
                and os.environ.get("AAP_JOBS_QUEUE_MODULE_LIVE") == "1"
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
            or (
                "tests/test_workspace_capacity_live.py" in selected_test_paths
                and os.environ.get("AAP_WORKSPACE_CAPACITY_LIVE") == "1"
            )
        ):
            phase = "install-workspace-api-consumer-fence-last"
            workspace_consumer_path = (
                ROOT / "infra/docker/workspace-consumer-grants.sql"
            )
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=workspace_consumer_path.read_text(),
                variables={
                    "expected_database": database,
                    "migration_role": "owner",
                    "workspace_owner": "workspace_code",
                    "consumer_role": IDENTITY_API_CONSUMER_ROLE,
                },
            )
            evidence["workspace_api_consumer"] = {
                "role": IDENTITY_API_CONSUMER_ROLE,
                "grant_path": str(workspace_consumer_path.relative_to(ROOT)),
                "sha256": hashlib.sha256(
                    workspace_consumer_path.read_bytes()
                ).hexdigest(),
            }
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
        identity_recording_live = (
            "tests/test_identity_recording_live.py" in selected_test_paths
            and os.environ.get("AAP_IDENTITY_RECORDING_FIXTURE_LIVE") == "1"
        )
        identity_e2e_live = (
            "tests/test_identity_e2e_live.py" in selected_test_paths
            and os.environ.get("AAP_IDENTITY_E2E_FIXTURE_LIVE") == "1"
        )
        if (
            media_seed_identity_live
            or identity_seed_selection_live
            or identity_recording_live
            or identity_e2e_live
        ):

            def workspace_import_cli_fence():
                nonlocal phase
                phase = "install-seed-identity-operator"
                persona_grants = (
                    ROOT
                    / "infra/docker"
                    / (
                        "identity-e2e-fixture-grants.sql"
                        if identity_e2e_live
                        else "identity-recording-fixture-grants.sql"
                        if identity_recording_live
                        else "identity-scale-seed-grants.sql"
                        if identity_seed_selection_live
                        else "identity-seed-operator-grants.sql"
                    )
                )
                for sql in (identity_consumer_grant_sql, persona_grants.read_text()):
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
                if identity_e2e_live:
                    for grant_name in (
                        "workspace-consumer-grants.sql",
                        "workspace-seed-operator-grants.sql",
                        "workspace-e2e-fixture-grants.sql",
                    ):
                        _psql(
                            container_id,
                            container_name,
                            container_identity,
                            username="owner",
                            password=owner_password,
                            database=database,
                            sql=(ROOT / "infra/docker" / grant_name).read_text(),
                            variables={
                                "expected_database": database,
                                "migration_role": "owner",
                                "workspace_owner": "workspace_code",
                                "consumer_role": "legacy_runtime",
                            },
                        )
                    evidence["workspace_e2e_fixture_operator"] = {
                        "role": "legacy_runtime",
                        "workspace_private_tables_denied": True,
                        "ordinary_api_fixture_capabilities_denied": True,
                        "profiles": [
                            "workspace-consumer-grants.sql",
                            "workspace-seed-operator-grants.sql",
                            "workspace-e2e-fixture-grants.sql",
                        ],
                    }
                if identity_seed_selection_live or identity_recording_live:
                    profiles = ["workspace-consumer-grants.sql"]
                    if identity_seed_selection_live:
                        profiles.append("workspace-scale-seed-grants.sql")
                    for profile in profiles:
                        _psql(
                            container_id,
                            container_name,
                            container_identity,
                            username="owner",
                            password=owner_password,
                            database=database,
                            sql=(ROOT / "infra/docker" / profile).read_text(),
                            variables={
                                "expected_database": database,
                                "migration_role": "owner",
                                "workspace_owner": "workspace_code",
                                "assets_owner": "assets_code",
                                "labeling_owner": "labeling_code",
                                "consumer_role": "legacy_runtime",
                            },
                        )
                    evidence[
                        "workspace_scale_seed_operator"
                        if identity_seed_selection_live
                        else "workspace_recording_consumer"
                    ] = {
                        "role": "legacy_runtime",
                        "workspace_private_tables_denied": True,
                        "profiles": profiles,
                    }
                evidence[
                    "identity_e2e_fixture_operator"
                    if identity_e2e_live
                    else "identity_recording_fixture_operator"
                    if identity_recording_live
                    else "identity_seed_selection_operator"
                    if identity_seed_selection_live
                    else "media_seed_identity_operator"
                ] = {
                    "role": "legacy_runtime",
                    "grant_path": str(persona_grants.relative_to(ROOT)),
                    "sha256": hashlib.sha256(persona_grants.read_bytes()).hexdigest(),
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
                workspace_import_grants = (
                    ROOT / "infra/docker/workspace-consumer-grants.sql"
                )
                _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="owner",
                    password=owner_password,
                    database=database,
                    sql=workspace_import_grants.read_text(),
                    variables={
                        "expected_database": database,
                        "migration_role": "owner",
                        "workspace_owner": "workspace_code",
                        "consumer_role": "legacy_runtime",
                    },
                )
                phase = "restore-workspace-import-cli-provisioning-capabilities"
                # The existing CLI provisions its operator in the same transaction.
                # The ordinary API role does not receive these trusted capabilities.
                provisioning_grants = """
    GRANT EXECUTE ON FUNCTION
        aap_identity_v1.read_provisioning_account(text),
        aap_identity_v1.create_provisioning_account(jsonb)
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
                            workspace_import_grants.read_bytes()
                        ).hexdigest(),
                    },
                    "provisioning_capabilities": [
                        "aap_identity_v1.read_provisioning_account(text)",
                        "aap_identity_v1.create_provisioning_account(jsonb)",
                    ],
                    "provisioning_grants_sha256": hashlib.sha256(
                        provisioning_grants.encode()
                    ).hexdigest(),
                    "other_domains": "existing trusted operator runtime grants retained",
                    "ordinary_api_unchanged": True,
                }
                if seed_operator_live:
                    phase = "install-workspace-seed-operator-capabilities"
                    seed_grants = (
                        ROOT / "infra/docker/workspace-seed-operator-grants.sql"
                    )
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql=seed_grants.read_text(),
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
                        "sha256": hashlib.sha256(seed_grants.read_bytes()).hexdigest(),
                        "ordinary_api_unchanged": True,
                        "project_private_tables_denied": True,
                        "other_domains": "existing trusted operator runtime grants retained",
                    }
                if seed_parent_live or workspace_media_seed_live:
                    phase = "install-identity-seed-operator-capabilities"
                    persona_grants = (
                        ROOT / "infra/docker/identity-seed-operator-grants.sql"
                    )
                    _psql(
                        container_id,
                        container_name,
                        container_identity,
                        username="owner",
                        password=owner_password,
                        database=database,
                        sql=persona_grants.read_text(),
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
                            persona_grants.read_bytes()
                        ).hexdigest(),
                        "ordinary_api_unchanged": True,
                    }

        phase = "verify-identity-api-consumer-acl"
        identity_consumer_acl = json.loads(
            _psql(
                container_id,
                container_name,
                container_identity,
                username="owner",
                password=owner_password,
                database=database,
                sql=f"""
SELECT pg_catalog.jsonb_build_object(
    'views', pg_catalog.jsonb_build_object(
        'account_profiles', pg_catalog.has_table_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.account_profiles', 'SELECT'),
        'invitation_audit_targets', pg_catalog.has_table_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.invitation_audit_targets', 'SELECT')),
    'functions', pg_catalog.jsonb_build_object(
        'read_actor_facts', pg_catalog.has_function_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.read_actor_facts(uuid)', 'EXECUTE'),
        'lock_accounts_share', pg_catalog.has_function_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.lock_accounts_share(uuid[],boolean)', 'EXECUTE'),
        'lock_accounts_update', pg_catalog.has_function_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.lock_accounts_update(uuid[],boolean)', 'EXECUTE'),
        'lock_accounts_key_share', pg_catalog.has_function_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.lock_accounts_key_share(uuid[],boolean)', 'EXECUTE'),
        'check_accepted_api_key', pg_catalog.has_function_privilege(
            '{IDENTITY_API_CONSUMER_ROLE}',
            'aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])', 'EXECUTE'))
)::text;
""",
            ).strip()
        )
        if identity_consumer_acl["views"] != {
            "account_profiles": True,
            "invitation_audit_targets": True,
        } or identity_consumer_acl["functions"] != {
            "read_actor_facts": True,
            "lock_accounts_share": True,
            "lock_accounts_update": True,
            "lock_accounts_key_share": True,
            "check_accepted_api_key": True,
        }:
            raise RuntimeError(
                "Identity API consumer is missing an approved read capability"
            )
        evidence["identity_api_consumer"] = {
            "role": IDENTITY_API_CONSUMER_ROLE,
            "grant_order": [
                {
                    "path": "infra/docker/lan-production-grants.sql",
                    "sha256": hashlib.sha256(
                        lan_production_grants_path.read_bytes()
                    ).hexdigest(),
                },
                {
                    "path": "infra/docker/identity-consumer-grants.sql",
                    "sha256": hashlib.sha256(
                        identity_consumer_grants_path.read_bytes()
                    ).hexdigest(),
                },
            ],
            "effective_access": identity_consumer_acl,
        }
        resource_environment.update(
            {
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
            "tests/test_labeling_video_writer_live.py",
            "tests/test_labeling_video_writer_containers.py",
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
            "tests/test_workspace_module_assembly_live.py",
            "tests/test_workspace_capacity_live.py",
            "tests/test_identity_e2e_live.py",
            "tests/test_identity_embedded_acl_live.py",
            "tests/test_worktree_identity_runtime_role_live.py",
            "tests/test_labeling_module_api_cold.py",
        }:
            api_bootstrap_storage = _cleanup_storage_scope(backend)
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

        resource_environment.update(
            {
                "AAP_MODULE_DATA_ROLE_ACCEPTANCE": "1",
                "AAP_IDENTITY_BRIEFS_LIVE_ACCEPTANCE": "1",
                "RUN_NATIVE_IMPORT_DELIVERY_LEASE_TEST": "1",
                "AAP_MODULE_DATA_APPEND_URL": append_url,
                "AAP_MODULE_DATA_NOTIFICATION_DELIVERY_READ_URL": notification_delivery_read_url,
                "AAP_MODULE_DATA_JOBS_LIST_QUERY_URL": jobs_list_query_url,
                "AAP_MODULE_DATA_JOBS_LIFECYCLE_URL": jobs_lifecycle_url,
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
                    username="preannotation_result_client",
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
            f"--junitxml={junit}",
            *test_files,
        ]
        options = parse_arguments(["exec", "--mode", "test", "--", *command])
        phase = "pytest"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        with patch.dict(os.environ, resource_environment, clear=False):
            prepare_owned_resources = backend.prepare

            def prepare_fenced_resources(*, skip_migrations=False):
                prepare_owned_resources(skip_migrations=skip_migrations)
                if workspace_import_cli_fence is not None:
                    workspace_import_cli_fence()
                # The launcher reapplies legacy table grants while preparing.
                # Reapply owner-bound reads and writes after broad test grants.
                apply_notification_preferences_grants()
                apply_notification_append_grants()
                apply_notification_delivery_read_grants()
                apply_notification_inbox_grants()
                apply_jobs_list_query_grants()
                apply_jobs_lifecycle_grants()
                apply_identity_actor_status_grants()
                apply_audit_append_grants()
                apply_notification_read_grants(NOTIFICATION_READ_ROLE)
                apply_audit_read_grants()
                apply_jobs_scope_grants()
                for statement in (
                    *notification_private_statements,
                    notification_fact_read_statement,
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
                notification_fact = _psql(
                    container_id,
                    container_name,
                    container_identity,
                    username="legacy_runtime",
                    password=runtime_password,
                    database=database,
                    sql=(
                        "SELECT count(*) FROM "
                        "aap_notifications_v1.read_notification("
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
                        "SELECT count(*) FROM aap_audit_v1.audit_events "
                        f"WHERE id='{audit_append_id}'::bigint;"
                    ),
                )
                if notification_fact.strip() != "1" or audit_fact.strip() != "1":
                    raise RuntimeError(
                        "module test preparation lost an owner read relation grant"
                    )

            with patch.object(backend, "prepare", prepare_fenced_resources):
                exit_code = run_processes(backend, options)
        evidence["test_exit_code"] = exit_code
        evidence["passed_test_count"] = _passed_test_count(junit)
        if exit_code == 0 and evidence["passed_test_count"] == 0:
            raise RuntimeError("Acceptance ran no passing test cases")
        evidence["status"] = "passed" if exit_code == 0 else "failed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = {"phase": phase, "error_type": type(error).__name__}
        if node_diagnostic is not None:
            evidence["failure"]["node_diagnostic"] = node_diagnostic
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
                cleanup_backend = backend
                if (
                    container_id
                    and "tests/test_legacy_video_fence_live.py" in selected_test_paths
                    and os.environ.get("AAP_LEGACY_VIDEO_FENCE_LIVE") == "1"
                ):
                    # This fixture deliberately disables every ordinary LOGIN.
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
