"""Freeze project annotations and coordinate durable dataset versions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from app.services import training_dataset_originals as originals
from app.services.content_snapshot import ContentError
from app.services.async_job_notify import notify_job_terminal
from app.services.project_access import resolve_project_access
from app.services.project_write_guard import lock_actor_project_authority
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    native_labels,
)
from training_contract.dataset import (
    SEGMENTATION,
    SEGMENTATION_CONTRACT,
    DETECTION_CONTRACT,
    INSTANCE_RLE_CONTRACT,
    DatasetContractError,
    instance_mask_annotations,
    is_instance_rle,
)
from app.services.training_dataset_insight_adapters import assert_parent_compatible

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
    if "managed_original_contract" in version.snapshot:
        try:
            await originals.delegated(db, version)
        except HTTPException as exc:
            if exc.status_code not in {401, 403}:
                raise
            code = (
                "api_key_revoked"
                if isinstance(exc.detail, dict)
                and exc.detail.get("code") == "api_key_authority_revoked"
                else "authority_revoked"
            )
            raise TrainingDatasetError(
                code, "训练数据版本的凭证或权限已失效", status_code=403
            ) from exc
        return
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
    """Legacy locator-only inputs cannot acquire a new original identity."""
    raise TrainingDatasetError(
        "original_input_unrecorded", "原图缺少已登记内容身份，请重新预检"
    )


@dataclass
class PreparedSelection:
    snapshot: dict
    report: dict
    authority: originals.Authority
    sources: dict
    selection: TrainingDatasetSelection


def check_prepared_clock(prepared):
    originals.clock(prepared.authority)


async def prepare_selection(
    db,
    project,
    selection,
    *,
    actor_id,
    api_key_id=None,
    http_request=None,
    trusted_actor=False,
):
    """Own read transactions without committing caller policy/version changes.

    Callers must release their business locks before this physical phase and
    reacquire their serialization/context guards before finalize_selection.
    """
    await db.flush()
    maker = async_sessionmaker(
        db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        async with maker() as reading:
            authority = await originals.authorize(
                reading,
                project.id,
                actor_id,
                http_request=http_request,
                api_key_id=api_key_id,
                trusted_actor=trusted_actor,
            )
            await reading.commit()
            current = await reading.get(Project, project.id, populate_existing=True)
            sources = {}
            snapshot, report = await _freeze_selection(
                reading,
                current,
                selection,
                actor_id=actor_id,
                api_key_id=api_key_id,
                authority=authority,
                observed=sources,
            )
            for source in sources.values():
                await originals.recheck(reading, authority, source)
            await originals.check(reading, authority)
            originals.clock(authority)
            return PreparedSelection(
                snapshot, report, authority, sources, selection.model_copy(deep=True)
            )
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            raise TrainingDatasetError(
                "authority_revoked", "训练数据请求的凭证或权限已失效", status_code=403
            ) from exc
        raise


async def finalize_selection(db, prepared):
    """Lock and compare the prepared selection without I/O or implicit commit."""
    from app.services import original_sources

    authority = prepared.authority
    await originals.check(db, authority)
    project = await db.get(Project, authority.project_id, populate_existing=True)
    task_ids = [uuid.UUID(s["task_id"]) for s in prepared.snapshot["samples"]]
    async with original_sources._reads(db, True):
        for statement in (
            select(Task.id).where(Task.id.in_(task_ids)).order_by(Task.id),
            select(Annotation.id)
            .where(Annotation.task_id.in_(task_ids))
            .order_by(Annotation.id),
        ):
            await db.execute(statement.with_for_update(read=True, nowait=True))
        for source in prepared.sources.values():
            await originals.recheck(db, authority, source)
        current, _report = await _freeze_selection(
            db,
            project,
            prepared.selection,
            actor_id=authority.actor_id,
            api_key_id=str(authority.key_id) if authority.key_id else None,
            authority=authority,
            frozen_samples={s["task_id"]: s for s in prepared.snapshot["samples"]},
        )
        if content_digest(current) != prepared.report["digest"]:
            raise TrainingDatasetError(
                "preflight_changed", "数据已变化，请重新预检", status_code=409
            )
        from app.services.managed_original_reclaim_fence import (
            protect_source,
            protect_snapshot_references,
        )

        for source in sorted(
            prepared.sources.values(),
            key=lambda source: (source.bucket or "", source.object_key),
        ):
            await protect_source(db, source)
        await protect_snapshot_references(db, current)
    originals.clock(authority)
    return prepared.snapshot, prepared.report


async def freeze_selection(
    db,
    project,
    selection,
    *,
    actor_id,
    api_key_id=None,
    http_request=None,
    trusted_actor=False,
):
    """Convenience for internal callers that already own the phase boundary."""
    prepared = await prepare_selection(
        db,
        project,
        selection,
        actor_id=actor_id,
        api_key_id=api_key_id,
        http_request=http_request,
        trusted_actor=trusted_actor,
    )
    if prepared.report["ready"]:
        return await finalize_selection(db, prepared)
    return prepared.snapshot, prepared.report


async def _freeze_selection(
    db: AsyncSession,
    project: Project,
    selection: TrainingDatasetSelection,
    *,
    actor_id: uuid.UUID,
    api_key_id: str | None = None,
    authority,
    frozen_samples=None,
    observed=None,
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
    contract = dict(
        INSTANCE_RLE_CONTRACT
        if selection.bridge == INSTANCE_RLE_CONTRACT["bridge"]
        else SEGMENTATION_CONTRACT
        if selection.task == SEGMENTATION
        else DETECTION_CONTRACT
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
            contract,
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
    if selection.dataset_id:
        from app.db.models.dataset import ProjectDataset

        if not await db.scalar(
            select(ProjectDataset.project_id).where(
                ProjectDataset.project_id == project.id,
                ProjectDataset.dataset_id == selection.dataset_id,
            )
        ):
            raise TrainingDatasetError(
                "dataset_not_linked", "数据集未关联当前项目", status_code=404
            )
        stmt = stmt.where(DatasetItem.dataset_id == selection.dataset_id)
    if selection.dataset_filter is not None:
        from app.services.dataset_workbench import project_filtered_item_ids

        actor = await db.get(User, actor_id, populate_existing=True)
        if actor is None or not actor.is_active:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建权限已撤销", status_code=403
            )
        item_ids = await project_filtered_item_ids(
            db, project.id, selection.dataset_id, selection.dataset_filter, actor
        )
        stmt = stmt.where(Task.dataset_item_id.in_(item_ids))
    if selection.task_ids:
        stmt = stmt.where(Task.id.in_(selection.task_ids))
    rows = (await db.execute(stmt.execution_options(populate_existing=True))).all()
    from app.services.reference_keys import is_reference

    if len(rows) > MAX_SAMPLES:
        raise TrainingDatasetError("sample_limit", "单个版本最多包含 20000 张图片")
    if selection.task_ids and {row[0].id for row in rows} != set(selection.task_ids):
        raise TrainingDatasetError(
            "task_not_found", "选中的任务不属于当前项目或批次", status_code=404
        )
    segmentation = selection.task == SEGMENTATION
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
    if selection.dataset_id:
        source["dataset_id"] = str(selection.dataset_id)
    dataset_selection = None
    if selection.dataset_filter is not None:
        dataset_selection = {
            "schema": 1,
            "dataset_id": str(selection.dataset_id),
            "filters": selection.dataset_filter.model_dump(
                mode="json", exclude_none=True
            ),
            "members": [
                {
                    "task_id": str(task.id),
                    "task_version": task.version,
                    "dataset_item_id": str(item.id),
                    "scene_id": str(item.scene_id) if item.scene_id else None,
                }
                for task, item, *_ in rows
            ],
        }
        source["dataset_selection"] = {
            "schema": 1,
            "filters": dataset_selection["filters"],
            "matched_tasks": len(rows),
            "digest": content_digest(dataset_selection),
        }
    samples, excluded, issues = [], [], []
    eligible_dataset_ids = set()
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
        reference = is_reference(task.file_path)
        if reference and item is None:
            issues.append(
                {
                    "task_id": task_id,
                    "code": "content_binding_changed",
                    "message": "引用样本身份已失效",
                }
            )
            continue
        try:
            if is_instance_rle(contract) and item and item.width and item.height:
                instance_mask_annotations(
                    active_annotations,
                    classes,
                    item.width if item else None,
                    item.height if item else None,
                )
            elif not is_instance_rle(contract):
                native_labels(active_annotations, classes, contract)
        except (TrainingDatasetError, DatasetContractError) as exc:
            issues.append({"task_id": task_id, "code": exc.code, "message": str(exc)})
            continue
        if item is not None:
            eligible_dataset_ids.add(item.dataset_id)
        samples.append(
            {
                **({"raster_contract": True} if is_instance_rle(contract) else {}),
                "task_id": task_id,
                "task_version": task.version,
                "annotations": active_annotations,
                "scene_id": str(item.scene_id) if item and item.scene_id else None,
                **(
                    {
                        "dataset_id": str(item.dataset_id),
                        "dataset_item_id": str(item.id),
                    }
                    if reference
                    else {}
                ),
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
    from app.services.dataset_lifecycle import require_writable

    dataset_revisions = {}
    for dataset_id in sorted(eligible_dataset_ids, key=str):
        dataset = await require_writable(db, dataset_id)
        dataset_revisions[dataset_id] = dataset.lifecycle_revision
    # Resolve declared content before any image I/O so the version budget is
    # enforced on authoritative sizes, not after downloading every sample.
    declared = 0
    for sample in samples:
        try:
            original_source = await originals.original_sources.resolve_task(
                db, uuid.UUID(sample["task_id"])
            )
            if is_reference(sample["source_key"]):
                declared += original_source.size_bytes
            else:
                original_source = await originals.pin(db, project.id, sample)
                sample["original"] = originals.receipt(original_source)
                declared += original_source.size_bytes
        except ContentError as exc:
            if exc.code.endswith("busy") or getattr(
                exc, "storage_operation_unsettled", False
            ):
                raise
            issues.append(
                {
                    "task_id": sample["task_id"],
                    "code": exc.code,
                    "message": "原图缺少可用的已登记内容身份",
                }
            )
    if declared > MAX_DATASET_BYTES:
        issues.append(
            {"code": "dataset_size_limit", "message": "训练数据总量超过 20 GiB"}
        )
    for sample in samples:
        if frozen_samples is not None:
            frozen = frozen_samples.get(sample["task_id"], {})
            for name in (
                "media",
                "asset_reference",
                "source_lineage",
                "source_maintenance",
            ):
                if name in frozen:
                    sample[name] = frozen[name]
            if is_instance_rle(contract) and sample.get("media"):
                sample["expected_width"] = sample.get("expected_width") or sample[
                    "media"
                ].get("width")
                sample["expected_height"] = sample.get("expected_height") or sample[
                    "media"
                ].get("height")
            continue
        if (
            is_reference(sample["source_key"])
            or "original" not in sample
            or declared > MAX_DATASET_BYTES
        ):
            continue
        try:
            sample["media"], original_source = await originals.preflight(
                db, authority, sample
            )
            observed[sample["task_id"]] = original_source
            if is_instance_rle(contract):
                sample["expected_width"] = (
                    sample.get("expected_width") or sample["media"]["width"]
                )
                sample["expected_height"] = (
                    sample.get("expected_height") or sample["media"]["height"]
                )
                instance_mask_annotations(
                    sample["annotations"],
                    classes,
                    sample["expected_width"],
                    sample["expected_height"],
                )
        except (TrainingDatasetError, DatasetContractError) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            issues.append(
                {"task_id": sample["task_id"], "code": exc.code, "message": str(exc)}
            )
        except ContentError as exc:
            if exc.code not in {
                "source_changed",
                "content_too_large",
                "content_media_unsupported",
            } or getattr(exc, "storage_operation_unsettled", False):
                raise
            issues.append(
                {
                    "task_id": sample["task_id"],
                    "code": exc.code,
                    "message": "原图内容无法通过完整校验",
                }
            )
        except (ValueError, OSError) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            issues.append(
                {
                    "task_id": sample["task_id"],
                    "code": "media_unavailable",
                    "message": "无法完整读取或解码原始图片",
                }
            )
    references = [s for s in samples if is_reference(s["source_key"])]
    if references and frozen_samples is None and declared <= MAX_DATASET_BYTES:
        from app.services.training_dataset_sources import training_reference_snapshot

        actor = await db.get(User, actor_id, populate_existing=True)
        # One AsyncSession cannot serve concurrent resolver queries.
        for sample in references:
            try:
                content, identity, lineage = await training_reference_snapshot(
                    db, sample, actor, api_key_id=api_key_id, freeze_lineage=True
                )
                try:
                    sample["asset_reference"] = identity
                    sample["source_lineage"] = lineage
                    sample["media"] = {
                        "sha256": content.sha256,
                        "size_bytes": content.size,
                    }
                    if is_instance_rle(contract):
                        instance_mask_annotations(
                            sample["annotations"],
                            classes,
                            content.width,
                            content.height,
                        )
                finally:
                    content.close()
            except (TrainingDatasetError, DatasetContractError) as exc:
                issues.append(
                    {
                        "task_id": sample["task_id"],
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
    if len(samples) < 2:
        issues.append(
            {"code": "insufficient_samples", "message": "至少需要两张合格图片"}
        )
    if (
        sum(
            s.get("media", {}).get("size_bytes", 0)
            + (
                sum(
                    annotation.get("geometry", {}).get("mask", {}).get("bytes", 0)
                    for annotation in s["annotations"]
                )
                if is_instance_rle(contract)
                else 0
            )
            for s in samples
        )
        > MAX_DATASET_BYTES
    ):
        issues.append(
            {"code": "dataset_size_limit", "message": "训练数据总量超过 20 GiB"}
        )
    snapshot = {
        **contract,
        "managed_original_contract": 1,
        **(
            {
                "asset_source_contract": {
                    "schema": 1,
                    "reader": "verified-asset-reference-1",
                    "freeze": "training-copy",
                },
                "source_lineage_contract": {
                    "schema": 1,
                    "scope": "accepted-reference",
                },
            }
            if references
            else {}
        ),
        **(
            {"dataset_selection": dataset_selection}
            if dataset_selection is not None
            else {}
        ),
        "annotation_scope": annotation_scope,
        "source": source,
        "classes": classes,
        "samples": samples,
        "seed": selection.seed,
        "validation_fraction": selection.validation_fraction,
        "selection": selection.canonical_payload(),
        "authorization": originals.authorization(authority),
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
            **(
                {
                    "reference_samples": len(references),
                    "original_copy_bytes": sum(
                        s.get("media", {}).get("size_bytes", 0) for s in samples
                    ),
                }
                if references
                else {}
            ),
        },
        "issues": sorted(issues, key=lambda i: (i.get("task_id", ""), i["code"])),
        "excluded": excluded,
    }
    if frozen_samples is not None and is_instance_rle(contract) and report["ready"]:
        # The caller inserts the immutable version and commits this same
        # transaction. GC rechecks roots while holding these exact content locks.
        from app.services.raster_mask_storage import lock_raster_mask_references

        await lock_raster_mask_references(db, snapshot)
    # Only a new selection is gated. Workers serving existing immutable versions
    # continue under their captured content and current project permissions.
    if frozen_samples is not None:
        for dataset_id, revision in dataset_revisions.items():
            await require_writable(
                db, dataset_id, expected_revision=revision, lock=True
            )
    if frozen_samples is not None and report["ready"] and references:
        from app.services.training_dataset_sources import lock_source_maintenance

        await lock_source_maintenance(db, snapshot)
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


async def dispatch_dataset(db: AsyncSession, version: TrainingDatasetVersion) -> None:
    from app.workers.training_dataset import run_training_dataset

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
        await asyncio.to_thread(
            run_training_dataset.apply_async,
            args=[str(version_id), str(job_id), str(attempt_id)],
            task_id=task_id,
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
    from app.db.models.storage_maintenance import StorageOperation

    if await db.scalar(
        select(StorageOperation.id)
        .where(StorageOperation.purpose == originals.output_purpose(version_id))
        .limit(1)
    ):
        raise TrainingDatasetError(
            "execution_stopping", "上次构建的存储操作尚未确认收尾", status_code=409
        )
