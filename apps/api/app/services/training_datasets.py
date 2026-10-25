"""Freeze project annotations and coordinate durable dataset versions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.schemas.training_dataset import TrainingDatasetCreate, TrainingDatasetSelection
from app.services import async_job as jobs
from app.services import project_task_dataset as task_datasets
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import JobTerminal
from app.services.project_access import get_workspace_project, resolve_project_access
from app.services.labeling_core_read_relations import batch_row, get_core_row
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
from app.services.training_authority_contract import (
    DEFAULT_TRAINING_SCOPES,
    TrainingAuthorityPort,
)
from app.services.dataset_task_facts import (
    ProjectTaskSourceRead,
    ProjectTaskSourceReader,
    thaw_source_json,
)
from app.services.job_execution_control import JobExecutionControl
from app.services.training_dataset_cancellation_contract import (
    TrainingDatasetCancellationFailure,
    TrainingDatasetCancellationPort,
)

log = logging.getLogger(__name__)


async def stage_version_deletion(
    db: AsyncSession, version: TrainingDatasetVersion
) -> None:
    """Stage SQL deletion; the workflow commits it with its storage intent."""
    await db.delete(version)


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


class SqlTrainingAuthority(TrainingAuthorityPort):
    """Bind the shared current-authority check to the caller's Session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def assert_authority(
        self,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        api_key_id: str | None = None,
        required_scopes: frozenset[str] = DEFAULT_TRAINING_SCOPES,
    ) -> None:
        if actor_id is None:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建者已不存在", status_code=403
            )
        actor = await lock_actor_project_authority(self._db, actor_id, project_id)
        project = await get_workspace_project(
            self._db,
            actor_id=actor_id,
            project_id=project_id,
            populate_existing=True,
        )
        if actor is None or project is None:
            raise TrainingDatasetError(
                "authority_revoked", "项目或创建者已不存在", status_code=403
            )
        try:
            access = await resolve_project_access(
                self._db, actor_id=actor.id, project=project
            )
        except HTTPException as exc:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建权限已撤销", status_code=403
            ) from exc
        if not access.is_manager:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建权限已撤销", status_code=403
            )
        if api_key_id:
            if not await IdentityAccountLockData(self._db).accepted_api_key_authorized(
                actor.id, uuid.UUID(api_key_id), required_scopes
            ):
                raise TrainingDatasetError(
                    "api_key_revoked", "训练数据请求的 API Key 已失效", status_code=403
                )


def bind_training_authority(db: AsyncSession) -> TrainingAuthorityPort:
    """Bind the authority port to the Session owned by HTTP or worker composition."""
    return SqlTrainingAuthority(db)


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
    source_reader: ProjectTaskSourceReader,
    actor_id: uuid.UUID,
    api_key_id: str | None = None,
) -> tuple[dict, dict]:
    if (project.data_type or "image") != "image":
        raise TrainingDatasetError(
            "unsupported_project", "当前训练数据版本仅支持图片项目"
        )
    if selection.batch_id:
        with db.no_autoflush:
            batch = await get_core_row(db, batch_row, selection.batch_id)
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
    segmentation = selection.task == SEGMENTATION
    tool_unit_id = "region" if segmentation else "bbox"
    source_facts = await source_reader.read_project_task_source_facts(
        ProjectTaskSourceRead(
            project_id=project.id,
            tool_unit_id=tool_unit_id,
            batch_id=selection.batch_id,
            task_ids=tuple(selection.task_ids),
            limit=MAX_SAMPLES + 1,
        )
    )
    task_facts = source_facts.tasks
    selected_count = len(task_facts)
    if selected_count > MAX_SAMPLES:
        raise TrainingDatasetError("sample_limit", "单个版本最多包含 20000 张图片")
    if selection.task_ids and {fact.task_id for fact in task_facts} != set(
        selection.task_ids
    ):
        raise TrainingDatasetError(
            "task_not_found", "选中的任务不属于当前项目或批次", status_code=404
        )
    contract = dict(SEGMENTATION_CONTRACT) if segmentation else dict(DETECTION_CONTRACT)
    bindings = source_facts.tool_bindings if task_facts else project.tool_bindings
    binding = bindings.get(tool_unit_id, {})
    if task_facts:
        binding = thaw_source_json(binding)
    del bindings, source_facts
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
    for fact in task_facts:
        task_id = str(fact.task_id)
        active_annotation_facts = sorted(
            fact.annotations, key=lambda annotation: annotation.id
        )
        excluded_annotations += fact.excluded_annotation_count
        reason = None
        if fact.file_type != "image":
            reason = "unsupported_media"
        elif fact.unusable:
            reason = "unusable_sample"
        elif selection.mode == "reviewed" and not fact.reviewed:
            reason = "not_reviewed"
        elif not active_annotation_facts and not (
            fact.reviewed_negative or task_id in negatives
        ):
            reason = "unconfirmed_negative"
        if reason:
            excluded.append({"task_id": task_id, "code": reason})
            continue
        active_annotations = [
            annotation.as_json() for annotation in active_annotation_facts
        ]
        try:
            native_labels(active_annotations, classes, contract)
        except TrainingDatasetError as exc:
            issues.append({"task_id": task_id, "code": exc.code, "message": str(exc)})
            continue
        samples.append(
            {
                "task_id": task_id,
                "task_version": fact.task_version,
                "annotations": active_annotations,
                "scene_id": str(fact.scene_id) if fact.scene_id else None,
                "source_bucket": storage_service.datasets_bucket
                if fact.dataset_item_id
                else storage_service.bucket,
                "source_key": fact.file_path,
                "expected_width": fact.width,
                "expected_height": fact.height,
                "eligibility": {
                    "mode": selection.mode,
                    "reviewed": fact.reviewed,
                    "negative": not active_annotation_facts,
                    "confirmed_by": str(actor_id),
                },
            }
        )
    del task_facts
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
            "selected": selected_count,
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


async def create_version(
    db: AsyncSession,
    project: Project,
    user: User | IdentityActorFacts,
    body: TrainingDatasetCreate,
    *,
    api_key_id: str | None,
    source_reader: ProjectTaskSourceReader,
    dispatcher: TrainingDatasetDispatcher,
) -> TrainingDatasetVersion:
    request_digest = content_digest(body.canonical_payload())
    # Serialize before taking share locks, avoiding share-to-exclusive upgrades.
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_project_manager(db, user.id, project.id)
    existing = (
        await db.execute(
            select(TrainingDatasetVersion).where(
                TrainingDatasetVersion.project_id == project.id,
                TrainingDatasetVersion.request_id == body.request_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(
                status_code=409, detail="同一请求标识不能用于不同数据版本"
            )
        await task_datasets.ensure_membership(db, existing, project_locked=True)
        await db.commit()
        return await task_datasets.attach_read_metadata(db, existing)

    snapshot, report = await freeze_selection(
        db,
        project,
        selection_of(body),
        source_reader=source_reader,
        actor_id=user.id,
        api_key_id=api_key_id,
    )
    if report["digest"] != body.preflight_digest:
        raise HTTPException(status_code=409, detail="数据已变化，请重新预检")
    if not report["ready"]:
        raise HTTPException(status_code=422, detail=report)
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=user.id,
        parent_id=body.parent_id,
        request_id=body.request_id,
        request_digest=request_digest,
        name=body.name.strip(),
        snapshot=snapshot,
        snapshot_digest=report["digest"],
        preparation_report=frozen_preparation_report(report["digest"], report),
    )
    db.add(version)
    await db.flush()
    await task_datasets.ensure_membership(db, version, project_locked=True)
    await enqueue_dataset(db, version)
    await db.commit()
    await dispatch_dataset(db, version, dispatcher=dispatcher)
    await db.refresh(version)
    return await task_datasets.attach_read_metadata(db, version)


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


def mark_dataset_running(version: TrainingDatasetVersion) -> None:
    version.status = "running"


def update_dataset_progress(version: TrainingDatasetVersion, progress_pct: int) -> None:
    version.progress_pct = progress_pct


def publish_dataset_package(
    version: TrainingDatasetVersion,
    *,
    manifest: dict,
    package_key: str,
    package_sha256: str,
    size_bytes: int,
) -> None:
    version.manifest = manifest
    version.package_key = package_key
    version.package_sha256 = package_sha256
    version.size_bytes = size_bytes
    version.status = "ready"
    version.progress_pct = 100
    version.completed_at = datetime.now(timezone.utc)


def fail_dataset_build(
    version: TrainingDatasetVersion,
    *,
    error_code: str,
    error_message: str,
) -> None:
    version.status = "failed"
    version.error_code = error_code
    version.error_message = error_message
    version.completed_at = datetime.now(timezone.utc)


async def create_policy_candidate_version(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    creator_id: uuid.UUID,
    parent_id: uuid.UUID,
    request_digest: str,
    name: str,
    snapshot: dict,
    snapshot_digest: str,
    preparation_report: dict,
) -> TrainingDatasetVersion:
    """Stage one policy candidate and its durable dataset work in this transaction."""
    version = TrainingDatasetVersion(
        project_id=project_id,
        creator_id=creator_id,
        parent_id=parent_id,
        request_id=uuid.uuid4(),
        request_digest=request_digest,
        name=name,
        snapshot=snapshot,
        snapshot_digest=snapshot_digest,
        preparation_report=preparation_report,
    )
    db.add(version)
    await db.flush()
    await task_datasets.ensure_membership(db, version, project_locked=True)
    await enqueue_dataset(db, version)
    return version


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
    jobs.bind_execution_id(job, task_id)
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


class SqlTrainingDatasetCancellation(TrainingDatasetCancellationPort):
    """Bind the public cancellation port to the caller-owned SQL transaction."""

    def __init__(
        self, db: AsyncSession, *, execution_control: JobExecutionControl
    ) -> None:
        self.__db = db
        self.__execution_control = execution_control

    async def request_cancel(
        self,
        *,
        job_id: uuid.UUID,
        project_id: uuid.UUID | None,
        actor_id: uuid.UUID,
    ) -> None:
        try:
            await lock_project_manager(
                self.__db,
                actor_id,
                project_id,
                denial_detail="需要项目管理权限",
            )
            version = (
                await self.__db.execute(
                    select(TrainingDatasetVersion)
                    .where(TrainingDatasetVersion.async_job_id == job_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            # The central composer holds the initial job in this same session,
            # so reacquiring it is an identity-map hit before the refresh.
            job = await self.__db.get(AsyncJob, job_id)
            await self.__db.refresh(job)
            if (
                version is None
                or version.status not in {"pending", "running"}
                or job.status not in {"pending", "running"}
            ):
                raise HTTPException(status_code=409, detail="训练数据构建已结束")
            if job.celery_task_id:
                try:
                    self.__execution_control.request_cancel(job.celery_task_id)
                except Exception:
                    log.exception("Training dataset revoke failed job=%s", job.id)
            await jobs.mark_cancelled(self.__db, job.id)
            await reconcile_job_terminal(
                self.__db, JobTerminal(job.id, job.kind, cancelled=True)
            )
            await notify_job_terminal(self.__db, job_id=job.id)
        except HTTPException as exc:
            raise TrainingDatasetCancellationFailure(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc
