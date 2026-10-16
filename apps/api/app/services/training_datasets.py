"""Freeze project annotations and coordinate durable dataset versions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.api_key import ApiKey
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import JobTerminal
from app.services.project_access import resolve_project_access
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_project_manager,
)
from app.services.storage import storage_service
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    native_labels,
)
from training_contract.dataset import (
    SEGMENTATION,
    SEGMENTATION_CONTRACT,
    DETECTION_CONTRACT,
)
from app.services.training_dataset_insight_adapters import assert_parent_compatible
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.job_execution_control import JobExecutionControl

log = logging.getLogger(__name__)

MAX_SAMPLES = 20000
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_DATASET_BYTES = 20 * 1024 * 1024 * 1024


def frozen_preparation_report(snapshot_digest: str, report: dict) -> dict:
    return {
        "schema": 1,
        "snapshot_digest": snapshot_digest,
        "preflight_digest": report["digest"],
        "summary": report["summary"],
        "excluded": report["excluded"],
        "issues": report["issues"],
    }


def selection_of(body) -> TrainingDatasetSelection:
    return TrainingDatasetSelection.model_validate(
        body.model_dump(include=set(TrainingDatasetSelection.model_fields))
    )


async def assert_dataset_authority(
    db: AsyncSession, version: TrainingDatasetVersion
) -> None:
    await assert_training_authority(
        db,
        project_id=version.project_id,
        actor_id=version.creator_id,
        api_key_id=version.snapshot.get("authorization", {}).get("api_key_id"),
    )


async def assert_training_authority(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    api_key_id: str | None = None,
    required_scopes: frozenset[str] = frozenset({"datasets:read", "annotations:read"}),
) -> None:
    if actor_id is None:
        raise TrainingDatasetError(
            "authority_revoked", "数据版本创建者已不存在", status_code=403
        )
    await lock_actor_project_authority(db, actor_id, project_id)
    actor = await db.get(User, actor_id, populate_existing=True)
    project = await db.get(Project, project_id, populate_existing=True)
    if actor is None or project is None:
        raise TrainingDatasetError(
            "authority_revoked", "项目或创建者已不存在", status_code=403
        )
    try:
        access = await resolve_project_access(db, user=actor, project=project)
    except HTTPException as exc:
        raise TrainingDatasetError(
            "authority_revoked", "数据版本创建权限已撤销", status_code=403
        ) from exc
    if not access.is_manager:
        raise TrainingDatasetError(
            "authority_revoked", "数据版本创建权限已撤销", status_code=403
        )
    key_id = api_key_id
    if key_id:
        key = (
            await db.execute(
                select(ApiKey)
                .where(ApiKey.id == uuid.UUID(key_id))
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            key is None
            or key.user_id != actor.id
            or key.revoked_at
            or (key.expires_at and key.expires_at <= datetime.now(timezone.utc))
            or ("*" not in key.scopes and not required_scopes <= set(key.scopes))
        ):
            raise TrainingDatasetError(
                "api_key_revoked", "训练数据请求的 API Key 已失效", status_code=403
            )


def capture_source_media(sample: dict) -> dict:
    head = storage_service.client.head_object(
        Bucket=sample["source_bucket"], Key=sample["source_key"]
    )
    size = head["ContentLength"]
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise TrainingDatasetError("image_size_limit", "图片大小超出训练数据范围")
    return {
        "etag": head["ETag"],
        "size_bytes": size,
        "version_id": head.get("VersionId"),
    }


async def freeze_selection(
    db: AsyncSession,
    project: Project,
    selection: TrainingDatasetSelection,
    *,
    actor_id: uuid.UUID,
    api_key_id: str | None = None,
) -> tuple[dict, dict]:
    if (project.data_type or "image") != "image":
        raise TrainingDatasetError(
            "unsupported_project", "当前训练数据版本仅支持图片项目"
        )
    if selection.batch_id:
        batch = await db.get(TaskBatch, selection.batch_id)
        if batch is None or batch.project_id != project.id:
            raise TrainingDatasetError(
                "batch_not_found", "批次不属于当前项目", status_code=404
            )
    if selection.parent_id:
        parent = await db.get(TrainingDatasetVersion, selection.parent_id)
        if parent is None or parent.project_id != project.id:
            raise TrainingDatasetError(
                "parent_not_found", "父数据版本不属于当前项目", status_code=404
            )
        if parent.status != "ready":
            raise TrainingDatasetError(
                "parent_not_ready", "父数据版本尚未就绪", status_code=409
            )
        assert_parent_compatible(
            parent.snapshot,
            parent.manifest,
            SEGMENTATION_CONTRACT
            if selection.task == SEGMENTATION
            else DETECTION_CONTRACT,
        )
    annotation_json = (
        select(
            func.jsonb_agg(
                func.jsonb_build_object(
                    "id",
                    cast(Annotation.id, String),
                    "version",
                    Annotation.version,
                    "class_name",
                    Annotation.class_name,
                    "tool_unit_id",
                    Annotation.tool_unit_id,
                    "geometry",
                    Annotation.geometry,
                )
            )
        )
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .correlate(Task)
        .scalar_subquery()
    )
    # Classes, task eligibility, media references and annotations share one PG
    # statement snapshot, even when the request session uses READ COMMITTED.
    stmt = (
        select(Task, DatasetItem, Project.tool_bindings, annotation_json)
        .join(Project, Project.id == Task.project_id)
        .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
        .where(Task.project_id == project.id)
        .order_by(Task.id)
        .limit(MAX_SAMPLES + 1)
    )
    if selection.batch_id:
        stmt = stmt.where(Task.batch_id == selection.batch_id)
    if selection.task_ids:
        stmt = stmt.where(Task.id.in_(selection.task_ids))
    rows = (await db.execute(stmt.execution_options(populate_existing=True))).all()
    if len(rows) > MAX_SAMPLES:
        raise TrainingDatasetError("sample_limit", "单个版本最多包含 20000 张图片")
    if selection.task_ids and {row[0].id for row in rows} != set(selection.task_ids):
        raise TrainingDatasetError(
            "task_not_found", "选中的任务不属于当前项目或批次", status_code=404
        )
    segmentation = selection.task == SEGMENTATION
    contract = dict(SEGMENTATION_CONTRACT) if segmentation else dict(DETECTION_CONTRACT)
    tool_unit_id = "region" if segmentation else "bbox"
    bindings = rows[0][2] if rows else project.tool_bindings
    binding = bindings.get(tool_unit_id, {})
    raw_classes = binding.get("classes", []) if binding.get("enabled") else []
    classes = [item["name"] if isinstance(item, dict) else item for item in raw_classes]
    if not classes or len(set(classes)) != len(classes):
        raise TrainingDatasetError(
            "invalid_classes",
            "项目需要启用对应的 region 多边形或矩形框工具，并配置不重复的类别",
        )
    annotation_scope = {
        "task": selection.task,
        "tool_unit_ids": [tool_unit_id],
        "classes": classes,
    }
    source = {
        "project_id": str(project.id),
        "media_type": project.data_type or "image",
        "tool_bindings": {tool_unit_id: binding},
    }
    samples, excluded, issues = [], [], []
    excluded_annotations = 0
    negatives = {str(value) for value in selection.confirmed_negative_ids}
    for task, item, _, active_annotations in rows:
        task_id = str(task.id)
        all_annotations = sorted(active_annotations or [], key=lambda a: a["id"])
        active_annotations = [
            a for a in all_annotations if a["tool_unit_id"] == tool_unit_id
        ]
        excluded_annotations += len(all_annotations) - len(active_annotations)
        current_skip = (
            task.skipped_at is not None and task.skipped_at == task.submitted_at
        )
        reviewed = task.status == "completed" and task.reviewed_at is not None
        reason = None
        if task.file_type != "image":
            reason = "unsupported_media"
        elif current_skip and task.skip_reason in {"image_corrupt", "unclear", "other"}:
            reason = "unusable_sample"
        elif selection.mode == "reviewed" and not reviewed:
            reason = "not_reviewed"
        elif not active_annotations and not (
            (
                reviewed
                and current_skip
                and task.skip_reason == "no_target"
                and not all_annotations
            )
            or task_id in negatives
        ):
            reason = "unconfirmed_negative"
        if reason:
            excluded.append({"task_id": task_id, "code": reason})
            continue
        try:
            native_labels(active_annotations, classes, contract)
        except TrainingDatasetError as exc:
            issues.append({"task_id": task_id, "code": exc.code, "message": str(exc)})
            continue
        samples.append(
            {
                "task_id": task_id,
                "task_version": task.version,
                "annotations": active_annotations,
                "scene_id": str(item.scene_id) if item and item.scene_id else None,
                "source_bucket": storage_service.datasets_bucket
                if task.dataset_item_id
                else storage_service.bucket,
                "source_key": task.file_path,
                "expected_width": item.width if item else None,
                "expected_height": item.height if item else None,
                "eligibility": {
                    "mode": selection.mode,
                    "reviewed": reviewed,
                    "negative": not active_annotations,
                    "confirmed_by": str(actor_id),
                },
            }
        )
    semaphore = asyncio.Semaphore(8)

    async def capture(sample):
        async with semaphore:
            try:
                sample["media"] = await asyncio.to_thread(capture_source_media, sample)
            except TrainingDatasetError as exc:
                issues.append(
                    {
                        "task_id": sample["task_id"],
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
            except Exception:
                issues.append(
                    {
                        "task_id": sample["task_id"],
                        "code": "media_unavailable",
                        "message": "无法读取原始图片",
                    }
                )

    await asyncio.gather(*(capture(sample) for sample in samples))
    if len(samples) < 2:
        issues.append(
            {"code": "insufficient_samples", "message": "至少需要两张合格图片"}
        )
    if (
        sum(s.get("media", {}).get("size_bytes", 0) for s in samples)
        > MAX_DATASET_BYTES
    ):
        issues.append(
            {"code": "dataset_size_limit", "message": "训练数据总量超过 20 GiB"}
        )
    snapshot = {
        **contract,
        "annotation_scope": annotation_scope,
        "source": source,
        "classes": classes,
        "samples": samples,
        "seed": selection.seed,
        "validation_fraction": selection.validation_fraction,
        "selection": selection.canonical_payload(),
        "authorization": {"actor_id": str(actor_id), "api_key_id": api_key_id},
    }
    report = {
        "task": selection.task,
        "media_type": contract["media_type"],
        "format": contract["format"],
        "annotation_scope": annotation_scope,
        "source": source,
        "ready": not issues,
        "digest": content_digest(snapshot),
        "classes": classes,
        "summary": {
            "selected": len(rows),
            "eligible": len(samples),
            "positives": sum(bool(s["annotations"]) for s in samples),
            "negatives": sum(not s["annotations"] for s in samples),
            "excluded": len(excluded),
            "excluded_annotations": excluded_annotations,
            "objects": sum(len(s["annotations"]) for s in samples),
        },
        "issues": sorted(issues, key=lambda i: (i.get("task_id", ""), i["code"])),
        "excluded": excluded,
    }
    return snapshot, report


async def enqueue_dataset(db: AsyncSession, version: TrainingDatasetVersion) -> None:
    job = await jobs.create_job(
        db,
        kind="training_dataset",
        user_id=version.creator_id,
        project_id=version.project_id,
        payload={"training_dataset_id": str(version.id)},
    )
    version.async_job_id = job.id
    version.attempt_id = uuid.uuid4()
    version.status = "pending"
    version.progress_pct = 0
    version.error_code = version.error_message = None
    version.completed_at = None
    await db.flush()


async def dispatch_dataset(
    db: AsyncSession,
    version: TrainingDatasetVersion,
    *,
    dispatcher: TrainingDatasetDispatcher,
) -> None:
    task_id = str(uuid.uuid4())
    version_id, job_id, attempt_id = (
        version.id,
        version.async_job_id,
        version.attempt_id,
    )
    job = await db.get(AsyncJob, version.async_job_id)
    job.celery_task_id = task_id
    await db.commit()
    try:
        await dispatcher.dispatch_dataset(
            version_id, job_id, attempt_id, execution_id=task_id
        )
    except Exception:
        await db.rollback()
        current = (
            await db.execute(
                select(TrainingDatasetVersion)
                .where(TrainingDatasetVersion.id == version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            current is not None
            and current.status == "pending"
            and current.async_job_id == job_id
            and current.attempt_id == attempt_id
        ):
            current.status = "failed"
            current.error_code = "dispatch_failed"
            current.error_message = "任务队列暂不可用，可重试同一数据快照"
            current.completed_at = datetime.now(timezone.utc)
            await jobs.mark_failed(db, job_id, error=current.error_message)
            await notify_job_terminal(db, job_id=job_id)
        await db.commit()


async def lock_dataset_execution(db: AsyncSession, version_id: uuid.UUID) -> None:
    """Fence deletion/retry against an execution still writing owned objects."""
    acquired = await db.scalar(
        select(
            func.pg_try_advisory_xact_lock(func.hashtextextended(str(version_id), 1))
        )
    )
    if not acquired:
        raise TrainingDatasetError(
            "execution_stopping", "上次构建仍在停止，请稍后重试", status_code=409
        )


async def reconcile_job_terminal(db: AsyncSession, terminal: JobTerminal) -> None:
    """Reconcile an interrupted build without changing an already-terminal version."""
    version = (
        await db.execute(
            select(TrainingDatasetVersion)
            .where(TrainingDatasetVersion.async_job_id == terminal.job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is not None and version.status in {"pending", "running"}:
        version.status = "cancelled" if terminal.cancelled else "failed"
        version.error_code = "cancelled" if terminal.cancelled else "worker_failed"
        version.error_message = (
            "训练数据构建已取消"
            if terminal.cancelled
            else "构建进程异常终止，可重试同一快照"
        )
        version.completed_at = datetime.now(timezone.utc)


async def request_job_cancel(
    db: AsyncSession,
    job: AsyncJob,
    user: User,
    *,
    execution_control: JobExecutionControl,
) -> None:
    """Fence dataset cancellation against a replaced or already-ended build."""
    await lock_project_manager(
        db, user, job.project_id, denial_detail="需要项目管理权限"
    )
    version = (
        await db.execute(
            select(TrainingDatasetVersion)
            .where(TrainingDatasetVersion.async_job_id == job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    await db.refresh(job)
    if (
        version is None
        or version.status not in {"pending", "running"}
        or job.status not in {"pending", "running"}
    ):
        raise HTTPException(status_code=409, detail="训练数据构建已结束")
    if job.celery_task_id:
        try:
            execution_control.request_cancel(job.celery_task_id)
        except Exception:
            log.exception("Training dataset revoke failed job=%s", job.id)
    await jobs.mark_cancelled(db, job.id)
    await reconcile_job_terminal(db, JobTerminal(job.id, job.kind, cancelled=True))
    await notify_job_terminal(db, job_id=job.id)
