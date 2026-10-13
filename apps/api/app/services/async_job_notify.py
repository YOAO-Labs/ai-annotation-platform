"""Terminal notification helper for user-visible async_jobs."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.notification import Notification
from app.services.notification import NotificationService


log = logging.getLogger(__name__)


TERMINAL_NOTIFY_KINDS = {
    "batch_predict",
    "video_tracker",
    "video_correction",
    "predictions_import",
    "audit_archive",
    "prediction_retry",
    "dataset_import",
    "mask_qc",
    "mask_repair",
    "mask_repair_rollback",
    "mask_format_import",
    "point_cloud_cross_frame",
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
    "issue_count",
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
