"""Celery producer adapter for the quality-job execution port."""

from __future__ import annotations

import uuid

from app.services.quality_job_dispatch import QualityRunKind


class CeleryQualityJobDispatcher:
    __slots__ = ()

    def dispatch_run(
        self, kind: QualityRunKind, run_id: uuid.UUID, *, execution_id: str
    ) -> None:
        if kind == "mask_qc":
            from app.workers.mask_qc import run_mask_qc as task
        elif kind == "point_cloud_quality":
            from app.workers.point_cloud_quality import run_point_cloud_quality as task
        elif kind == "video_track_quality":
            from app.workers.video_track_quality import run_video_track_quality as task
        else:
            raise ValueError(f"Unsupported quality run kind: {kind}")
        task.apply_async(args=[str(run_id)], task_id=execution_id)

    def dispatch_mask_repair(
        self, batch_id: uuid.UUID, *, rollback: bool, execution_id: str
    ) -> str:
        from app.workers.mask_repair import rollback_mask_repair, run_mask_repair

        task = rollback_mask_repair if rollback else run_mask_repair
        result = task.apply_async(
            args=[str(batch_id)], queue="media", task_id=execution_id
        )
        return str(result.id)

    def dispatch_mask_import(self, batch_id: uuid.UUID, *, execution_id: str) -> str:
        from app.workers.mask_format_import import run_mask_format_import

        result = run_mask_format_import.apply_async(
            args=[str(batch_id)], task_id=execution_id
        )
        return str(result.id)
