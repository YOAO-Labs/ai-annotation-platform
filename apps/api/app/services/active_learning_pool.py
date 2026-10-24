"""Current unlabeled sources and immutable historical exclusion membership."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.exc import DBAPIError

from app.db.models.active_learning import (
    ActiveLearningPoolMember as Member,
    ActiveLearningReference as Reference,
    ActiveLearningRound as Round,
)
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.sample_evaluation import SampleEvaluationRun as Evaluation
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.db.models.training_dataset import TrainingDatasetVersion as Version
from app.services import active_learning_engine as strategy
from app.services import sample_evaluation_engine as detection
from app.services.managed_inference import qualification, reject
from app.services.project_write_guard import _busy_lock_error
from app.services import active_learning_originals as originals
from app.services.task_lock import TaskLockConflictError, TaskLockService
from app.services.training_dataset_bridge import (
    content_digest,
    sample_identities,
    split_history,
)
from app.services.training_datasets import assert_training_authority

MAX_TOTAL_RAW = 128 * 1024 * 1024
MAX_HISTORICAL_BYTES = 16 * 1024 * 1024
MAX_HISTORICAL_SOURCES = 256
SCOPES = originals.SCOPES


def eligibility():
    return and_(
        Task.file_type == "image",
        Task.status == "pending",
        Task.is_labeled.is_(False),
        Task.total_annotations == 0,
        Task.assignee_id.is_(None),
        Task.reviewer_id.is_(None),
        Task.submitted_at.is_(None),
        Task.reviewed_at.is_(None),
        Task.skipped_at.is_(None),
        Task.skip_reason.is_(None),
        Task.first_reviewed_at.is_(None),
        Task.first_review_result.is_(None),
        Task.reopened_count == 0,
        or_(
            Task.batch_id.is_(None),
            and_(
                TaskBatch.display_id == "B-DEFAULT",
                TaskBatch.project_id == Task.project_id,
                TaskBatch.status != "archived",
                TaskBatch.admin_locked.is_(False),
                TaskBatch.annotator_id.is_(None),
                TaskBatch.reviewer_id.is_(None),
                TaskBatch.assigned_user_ids == [],
            ),
        ),
        ~select(Annotation.id)
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .exists(),
        ~select(AnnotationDraft.id).where(AnnotationDraft.task_id == Task.id).exists(),
        ~select(TaskLock.id)
        .where(
            TaskLock.task_id == Task.id, TaskLock.expire_at > datetime.now(timezone.utc)
        )
        .exists(),
    )


def query(project_id):
    return (
        select(Task, DatasetItem, TaskBatch)
        .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(Task.project_id == project_id, eligibility())
        .execution_options(populate_existing=True)
    )


def source_snapshot(task, item, batch):
    fields = (
        "display_id",
        "file_name",
        "file_path",
        "file_type",
        "status",
        "version",
        "is_labeled",
        "total_annotations",
        "assignee_id",
        "reviewer_id",
        "assignee_is_override",
        "reviewer_is_override",
        "assigned_at",
        "submitted_at",
        "reviewed_at",
        "skipped_at",
        "skip_reason",
        "review_round_id",
        "first_reviewed_at",
        "first_review_result",
        "reopened_count",
        "tags",
    )

    def plain(value):
        return (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, uuid.UUID)
            else value
        )

    return {
        "task_id": str(task.id),
        "project_id": str(task.project_id),
        "dataset_item_id": str(item.id) if item else None,
        "scene_id": str(item.scene_id) if item and item.scene_id else None,
        "source_key": task.file_path,
        "file_type": task.file_type,
        "expected_width": item.width if item else None,
        "expected_height": item.height if item else None,
        "task": {key: plain(getattr(task, key)) for key in fields},
        "item": {
            key: plain(getattr(item, key))
            for key in (
                "id",
                "dataset_id",
                "file_path",
                "file_type",
                "file_size",
                "content_hash",
                "width",
                "height",
                "scene_id",
                "frame_index",
            )
        }
        if item
        else None,
        "batch": {
            key: plain(getattr(batch, key))
            for key in (
                "id",
                "display_id",
                "status",
                "admin_locked",
                "annotator_id",
                "reviewer_id",
                "assigned_user_ids",
            )
        }
        if batch
        else None,
    }


async def snapshots(db, project_id, task_ids):
    rows = (
        await db.execute(
            query(project_id).where(Task.id.in_(task_ids)).order_by(Task.id)
        )
    ).all()
    if len(rows) != len(task_ids):
        raise reject(
            "pool_stale", "候选须为未分派、无标注或草稿且未被编辑锁定的图片任务"
        )
    result = [source_snapshot(*row) for row in rows]
    if any(
        row["item"]
        and (
            row["item"]["file_path"] != row["source_key"]
            or row["item"]["file_type"] != "image"
        )
        for row in result
    ):
        raise reject("source_changed", "任务与素材图片引用不一致")
    return [await originals.pin(db, snapshot) for snapshot in result]


async def lock_unedited(db, task_ids, actor_id):
    locks = TaskLockService(db)
    for task_id in sorted(task_ids):
        acquired = await db.scalar(
            select(
                func.pg_try_advisory_xact_lock(
                    func.hashtextextended(f"aap:task-edit-lock:{task_id}", 0)
                )
            )
        )
        if not acquired:
            raise reject("pool_busy", "候选编辑锁正在变化，请稍后重新检查")
        try:
            await locks.assert_write_allowed(task_id, actor_id)
        except TaskLockConflictError as exc:
            raise reject("pool_locked", "候选正在编辑，请关闭工作台后重新选样") from exc
        if await locks.active_lock(task_id) is not None:
            raise reject("pool_locked", "候选正在编辑；自己的工作台锁也须先释放")


async def lock_source_items(db, item_ids):
    if not item_ids:
        return
    try:
        await db.execute(
            select(DatasetItem.id)
            .where(DatasetItem.id.in_(item_ids))
            .order_by(DatasetItem.id)
            .with_for_update(read=True, nowait=True)
        )
    except DBAPIError as exc:
        if _busy_lock_error(exc):
            raise reject("source_busy", "候选素材正在变化，请稍后重新检查") from exc
        raise


async def list_pool(db, project_id, limit, offset):
    total = await db.scalar(
        select(func.count()).select_from(query(project_id).subquery())
    )
    rows = (
        await db.execute(
            query(project_id)
            .order_by(Task.created_at, Task.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "task_id": str(task.id),
                "display_id": task.display_id,
                "file_name": task.file_name,
                "dataset_item_id": str(item.id) if item else None,
                "scene_id": str(item.scene_id) if item and item.scene_id else None,
            }
            for task, item, _batch in rows
        ],
    }


async def exclusions(db, project_id, *, lock=False):
    """Fail closed at the history budget; never silently drop old membership."""
    clauses = [Version.project_id == project_id]
    version_sizes = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            func.octet_length(cast(Version.snapshot, String)), 0
                        )
                        + func.coalesce(
                            func.octet_length(cast(Version.manifest, String)), 0
                        )
                    ),
                    0,
                ),
            ).where(*clauses)
        )
    ).one()
    evaluation_sizes = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(func.octet_length(cast(Evaluation.spec, String))), 0
                ),
            ).where(
                Evaluation.project_id == project_id,
                Evaluation.spec["role"].astext == "held_out_test",
            )
        )
    ).one()
    if (
        version_sizes[0] + evaluation_sizes[0] > MAX_HISTORICAL_SOURCES
        or version_sizes[1] + evaluation_sizes[1] > MAX_HISTORICAL_BYTES
    ):
        raise reject(
            "history_budget",
            "项目保留历史超过本期有界排除检查范围，未省略任何历史成员",
            422,
        )
    versions_query = select(Version).where(*clauses).order_by(Version.id)
    evals_query = (
        select(Evaluation)
        .where(
            Evaluation.project_id == project_id,
            Evaluation.spec["role"].astext == "held_out_test",
        )
        .order_by(Evaluation.id)
    )
    if lock:
        versions_query = versions_query.with_for_update(read=True)
        evals_query = evals_query.with_for_update(read=True)
    versions = (
        await db.scalars(versions_query.execution_options(populate_existing=True))
    ).all()
    evaluations = (
        await db.scalars(evals_query.execution_options(populate_existing=True))
    ).all()
    identities, refs = set(), []
    for version in versions:
        if content_digest(version.snapshot) != version.snapshot_digest:
            raise reject("history_corrupt", "保留的训练数据快照未通过身份校验")
        manifest = version.manifest or {}
        digest = manifest.get("content_digest")
        membership = split_history(manifest)
        expected = {
            f"task:{sample['task_id']}"
            for sample in version.snapshot.get("samples", [])
        }
        expected.update(
            f"scene:{sample['scene_id']}"
            for sample in version.snapshot.get("samples", [])
            if sample.get("scene_id")
        )
        if (
            not digest
            or manifest.get("source_digest") != version.snapshot_digest
            or not expected <= set(membership)
        ):
            raise reject(
                "history_unready",
                "保留的数据版本尚无覆盖全部成员的内容清单；请先完成构建或移除未使用的失败版本",
            )
        if manifest and (
            not digest
            or content_digest(
                {k: v for k, v in manifest.items() if k != "content_digest"}
            )
            != digest
        ):
            raise reject("history_corrupt", "保留的训练数据清单未通过身份校验")
        for sample in version.snapshot.get("samples", []):
            identities.add(f"task:{sample['task_id']}")
            if sample.get("scene_id"):
                identities.add(f"scene:{sample['scene_id']}")
        if any(value not in {"train", "val"} for value in membership.values()):
            raise reject("history_corrupt", "历史训练与验证成员分区身份无效")
        identities.update(membership)
        for sample in manifest.get("samples", []):
            identities.update(sample_identities(sample))
        refs.append(
            {
                "kind": "dataset",
                "id": str(version.id),
                "digest": content_digest(
                    {
                        "snapshot_digest": version.snapshot_digest,
                        "manifest_digest": digest,
                    }
                ),
            }
        )
    for evaluation in evaluations:
        if (
            content_digest(evaluation.spec) != evaluation.spec_digest
            or evaluation.deletion_requested_at
        ):
            raise reject("history_corrupt", "保留的独立测试规格已变化或正在删除")
        for sample in evaluation.spec["samples"]:
            identities.update(sample_identities(sample))
        refs.append(
            {
                "kind": "evaluation",
                "id": str(evaluation.id),
                "digest": evaluation.spec_digest,
            }
        )
    result = {"identities": sorted(identities), "references": refs}
    return {**result, "digest": content_digest(result)}


async def prepare(db, project, body, actor_id, api_key_id=None, *, request):
    context = await originals.authorize(db, project.id, actor_id, request, api_key_id)
    # External qualification and media IO precede final domain locks.
    ref, profile = await qualification(
        db, project, body.model_version_id, body.deployment_id
    )
    await assert_training_authority(
        db,
        project_id=project.id,
        actor_id=actor_id,
        api_key_id=api_key_id,
        required_scopes=SCOPES,
    )
    before = await snapshots(db, project.id, body.sample_task_ids)
    history = await exclusions(db, project.id)
    await db.commit()
    verified, live_sources, total = [], [], 0
    for snapshot in before:
        try:
            _png, media, source = await originals.read_input(db, context, snapshot)
            live_sources.append(source)
        except originals.ContentError:
            raise
        except (ValueError, OSError) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise reject(
                "source_unavailable",
                "候选图片未通过条件读取、解码、大小或身份检查",
                422,
            ) from exc
        total += media["size_bytes"]
        if strategy.strategy_for(ref) == strategy.MASK_STRATEGY:
            from app.services import sample_evaluation_masks as masks

            if (
                media["width"] * media["height"] > masks.MAX_PIXELS
                or max(media["width"], media["height"]) > masks.MAX_DIMENSION
            ):
                raise reject("source_budget", "候选原图超过分割评估协议的像素预算", 422)
        if total > MAX_TOTAL_RAW:
            raise reject("source_budget", "候选图片总量超过 128 MiB", 422)
        verified.append(
            {
                "task_id": snapshot["task_id"],
                "scene_id": snapshot["scene_id"],
                "snapshot": snapshot,
                **media,
            }
        )
    await assert_training_authority(
        db,
        project_id=project.id,
        actor_id=actor_id,
        api_key_id=api_key_id,
        required_scopes=SCOPES,
    )
    if before != await snapshots(
        db, project.id, body.sample_task_ids
    ) or history != await exclusions(db, project.id):
        raise reject("pool_stale", "核验期间候选来源或历史成员已变化，请重新预览")
    for source in live_sources:
        await originals.recheck(db, context, source, commit=False)
    samples, omitted = strategy.grouped_pool(verified, history["identities"])
    samples = strategy.partition(samples, body.seed, body.batch_size)
    if body.max_inference_dispatches < len(samples):
        raise reject("inference_budget", "推理预算至少须覆盖去重后的全部独立候选", 422)
    result = {
        "schema": 1,
        "original_contract": 1,
        "pool": verified,
        "pool_digest": content_digest(verified),
        "samples": samples,
        "omitted": omitted,
        "exclusions": history,
        "model_ref": ref,
        "profile": profile,
        "profile_digest": profile["profile_digest"],
        "strategy": strategy.strategy_for(ref),
        "preprocessing": detection.PREPROCESSING,
        **body.model_dump(
            mode="json",
            include={
                "seed",
                "batch_size",
                "max_inference_dispatches",
                "max_review_tasks",
                "confidence",
                "nms_iou",
            },
        ),
    }
    if strategy.strategy_for(ref) == strategy.MASK_STRATEGY:
        result.update(schema=2, schema_version=2, task="segment")
    await db.commit()
    originals.access.check_clock(context.proof, context.key)
    return {
        **result,
        "preview_digest": content_digest(result),
        "_authority": context,
        "_live_sources": live_sources,
    }


async def guard_source(
    db,
    *,
    project_id=None,
    dataset_id=None,
    item_ids=None,
    task_ids=None,
    version_id=None,
    evaluation_id=None,
):
    """Call before any destructive object or child-row mutation."""
    if project_id is not None:
        found = await db.scalar(
            select(Round.id).where(Round.project_id == project_id).limit(1)
        )
    elif version_id is not None or evaluation_id is not None:
        condition = (
            Reference.dataset_version_id == version_id
            if version_id is not None
            else Reference.evaluation_id == evaluation_id
        )
        found = await db.scalar(select(Reference.id).where(condition).limit(1))
    else:
        query_ref = select(Member.round_id)
        if dataset_id is not None:
            query_ref = query_ref.join(
                DatasetItem, DatasetItem.id == Member.dataset_item_id
            ).where(DatasetItem.dataset_id == dataset_id)
        elif item_ids is not None:
            query_ref = query_ref.where(Member.dataset_item_id.in_(item_ids))
        elif task_ids is not None:
            query_ref = query_ref.where(Member.task_id.in_(task_ids))
        else:
            raise ValueError("A specific active learning source is required")
        found = await db.scalar(query_ref.limit(1))
    if found:
        raise reject(
            "active_learning_reference",
            "主动学习历史仍引用此来源；未删除数据或存储对象",
        )
