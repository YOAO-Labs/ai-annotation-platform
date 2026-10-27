"""Terminal notification helper for user-visible async_jobs."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.notification import Notification
from app.services.notification import NotificationService


log = logging.getLogger(__name__)


TERMINAL_NOTIFY_KINDS = {
    "training_dataset",
    "training_run",
    "sample_evaluation",
    "active_learning_round",
    "batch_predict",
    "video_tracker",
    "video_correction",
    "predictions_import",
    "audit_archive",
    "prediction_retry",
    "dataset_import",
    "dataset_scan",
    "dataset_registration",
    "storage_usage",
    "video_asset_retry",
    "managed_original_verification",
    "managed_original_reclaim",
    "managed_reference_reconciliation",
    "storage_migration_inventory",
    "storage_migration_copy",
    "dataset_quality",
    "dataset_quality_review",
    "mask_qc",
    "mask_repair",
    "mask_repair_rollback",
    "mask_format_import",
    "point_cloud_cross_frame",
    "point_cloud_quality",
}

_STATUS_TO_TYPE = {
    AsyncJobStatus.COMPLETED.value: "job.completed",
    AsyncJobStatus.FAILED.value: "job.failed",
    AsyncJobStatus.CANCELLED.value: "job.cancelled",
}

_PAYLOAD_SUMMARY_KEYS = (
    "batch_display_id",
    "task_display_id",
    "project_display_id",
    "project_name",
    "ml_backend_name",
    "failed_prediction_id",
    "task_display_id",
    "error_type",
    "output_mode",
    "format",
    "dataset_id",
    "dataset_display_id",
    "dataset_name",
    "connection_name",
    "connection_kind",
    "total_tasks",
    "total_files",
    "from_frame",
    "to_frame",
    "model_key",
    "direction",
    "retain_months",
    "source_snapshot_digest",
    "mask_repair_batch_id",
    "mask_format_import_id",
    "staged_sha256",
    "plan_digest",
    "scene_name",
    "scope",
)

_RESULT_SUMMARY_KEYS = (
    "applied_count",
    "conflict_count",
    "blocked_count",
    "partial",
    "processed_count",
    "directory_count",
    "complete",
    "success_count",
    "failed_count",
    "done_count",
    "skipped_count",
    "cancelled_at_index",
    "duration_ms",
    "total_cost",
    "prediction_id",
    "reason",
    "imported",
    "added",
    "skipped",
    "total",
    "error_count",
    "file_count",
    "size_bytes",
    "cache_hit",
    "archived",
    "deleted",
    "run_id",
    "evaluation_id",
    "round_id",
    "attempt_id",
    "issue_count",
    "checked_count",
    "duplicate_group_count",
    "duplicate_member_count",
    "result_digest",
    "rolled_back_count",
    "stale_count",
    "created_annotation_count",
)


def _copy_present(
    out: dict[str, Any], source: dict[str, Any], keys: tuple[str, ...]
) -> None:
    for key in keys:
        if key in source:
            out[key] = source[key]


def _terminal_payload(job: AsyncJob) -> dict[str, Any]:
    payload_in = job.payload or {}
    result_in = job.result or {}
    payload: dict[str, Any] = {
        "kind": job.kind,
        "status": job.status,
    }
    if job.kind == "managed_original_reclaim":
        for key in ("reclaim_id", "retirement_id"):
            value = payload_in.get(key)
            try:
                payload[key] = str(uuid.UUID(value))
            except (TypeError, ValueError, AttributeError):
                pass
        if result_in.get("outcome") in {
            "deleted",
            "already_absent",
            "missing",
            "changed",
            "failed",
            "unknown",
            "cancelled",
        }:
            payload["outcome"] = result_in["outcome"]
        for key in ("delete_started", "absence_proven", "physical_settled"):
            if type(result_in.get(key)) is bool:
                payload[key] = result_in[key]
        return payload
    if job.kind == "storage_migration_copy":
        from app.schemas.storage_migration_copy import StorageMigrationCopyCounts

        payload["migration_ready"] = False
        for key in ("prepared", "copy_subset_complete", "attempt_subset_complete"):
            if type(result_in.get(key)) is bool:
                payload[key] = result_in[key]
        for key in ("selected_count", "selected_verified_count"):
            if type(result_in.get(key)) is int and result_in[key] >= 0:
                payload[key] = result_in[key]
        if result_in.get("operation") in {"prepare", "run", "verify"}:
            payload["operation"] = result_in["operation"]
        try:
            payload["copy_id"] = str(uuid.UUID(result_in["copy_id"]))
        except (ValueError, TypeError, KeyError):
            pass
        try:
            payload["counts"] = StorageMigrationCopyCounts.model_validate(
                result_in.get("counts")
            ).model_dump()
        except ValueError:
            pass
        code = result_in.get("error_code")
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            payload["error_code"] = code
        return payload
    if job.kind == "storage_migration_inventory":
        # This global private-report task has a deliberately closed summary.
        from app.schemas.storage_migration_inventory import (
            StorageMigrationInventoryCounts,
        )

        payload["scope"] = "selected-configured-source"
        payload["migration_ready"] = False
        counts = result_in.get("counts")
        if counts is not None:
            try:
                payload["counts"] = StorageMigrationInventoryCounts.model_validate(
                    counts
                ).model_dump()
            except ValueError:
                pass
        for key in ("complete", "report_available"):
            if type(result_in.get(key)) is bool:
                payload[key] = result_in[key]
        sha = result_in.get("report_sha256")
        if (
            isinstance(sha, str)
            and len(sha) == 64
            and all(c in "0123456789abcdef" for c in sha)
        ):
            payload["report_sha256"] = sha
        code = result_in.get("error_code")
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            payload["error_code"] = code
        return payload
    if job.project_id is not None:
        payload["project_id"] = str(job.project_id)
    _copy_present(payload, payload_in, _PAYLOAD_SUMMARY_KEYS)
    _copy_present(payload, result_in, _RESULT_SUMMARY_KEYS)
    if job.error_message:
        payload["error_message"] = job.error_message[:200]
    return payload


async def notify_job_terminal(db: AsyncSession, *, job_id: uuid.UUID) -> None:
    """Emit a generic user notification for a whitelisted terminal async job.

    Callers keep transaction ownership: invoke this after mark_complete/failed/cancelled
    and before the caller's commit. Notification failures are logged and do not affect
    the job state transition.
    """
    try:
        job = await db.get(AsyncJob, job_id)
        if job is None:
            return
        if job.user_id is None:
            return
        if job.kind not in TERMINAL_NOTIFY_KINDS:
            return

        notif_type = _STATUS_TO_TYPE.get(job.status)
        if notif_type is None:
            return

        existing = (
            await db.execute(
                select(Notification.id)
                .where(
                    Notification.user_id == job.user_id,
                    Notification.type == notif_type,
                    Notification.target_type == "async_job",
                    Notification.target_id == job.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        # This helper's documented contract is "write + publish before the
        # caller's commit" across 60+ worker call sites.  The WS delivery gate
        # briefly retries when the row is not yet visible in its own session, so
        # the message is delivered once the caller's commit lands instead of
        # being dropped; publish only after commit is not possible here without
        # touching every caller.
        await NotificationService(db).notify(
            user_id=job.user_id,
            type=notif_type,
            target_type="async_job",
            target_id=job.id,
            payload=_terminal_payload(job),
            defer_publish=False,
        )
    except Exception:  # noqa: BLE001
        log.exception("async job terminal notification failed job=%s", job_id)
