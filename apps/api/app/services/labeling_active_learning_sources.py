"""Labeling-owned current candidate reads and task edit fences."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.services.labeling_active_learning_source_contract import (
    CandidateBatchState,
    CandidateEditFence,
    CandidateItemState,
    CandidatePageRead,
    CandidatePoolPage,
    CandidatePoolItem,
    CandidateSourceError,
    CandidateSourceSnapshot,
    CandidateSourcesRead,
    CandidateTaskState,
    LabelingActiveLearningSources,
)
from app.services.task_lock import TaskLockConflictError, TaskLockService
from app.services.labeling_scene import read_spatial_item_facts


class SqlAlchemyLabelingActiveLearningSources(LabelingActiveLearningSources):
    """Bind Labeling's batch read and local edit fences to one existing session."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        dataset_bucket: str,
        task_bucket: str,
    ) -> None:
        self._db = db
        self._dataset_bucket = dataset_bucket
        self._task_bucket = task_bucket

    @staticmethod
    def _eligibility():
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
            ~select(AnnotationDraft.id)
            .where(AnnotationDraft.task_id == Task.id)
            .exists(),
            ~select(TaskLock.id)
            .where(
                TaskLock.task_id == Task.id,
                TaskLock.expire_at > datetime.now(timezone.utc),
            )
            .exists(),
        )

    def _candidate_query(self, project_id: uuid.UUID):
        return (
            select(Task, DatasetItem, TaskBatch)
            .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
            .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
            .where(Task.project_id == project_id, self._eligibility())
            .options(
                load_only(
                    DatasetItem.id,
                    DatasetItem.dataset_id,
                    DatasetItem.file_path,
                    DatasetItem.file_type,
                    DatasetItem.file_size,
                    DatasetItem.content_hash,
                    DatasetItem.width,
                    DatasetItem.height,
                )
            )
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _plain(value):
        return (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, uuid.UUID)
            else value
        )

    def _source_snapshot(
        self, task, item, batch, spatial_item
    ) -> CandidateSourceSnapshot:
        task_fields = (
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
        task_state: CandidateTaskState = {
            key: (
                copy.deepcopy(getattr(task, key))
                if key == "tags"
                else self._plain(getattr(task, key))
            )
            for key in task_fields
        }
        item_state: CandidateItemState | None = (
            {
                key: self._plain(getattr(item, key))
                for key in (
                    "id",
                    "dataset_id",
                    "file_path",
                    "file_type",
                    "file_size",
                    "content_hash",
                    "width",
                    "height",
                )
            }
            if item
            else None
        )
        if item_state is not None:
            item_state["scene_id"] = (
                str(spatial_item.scene_id)
                if spatial_item is not None and spatial_item.scene_id is not None
                else None
            )
            item_state["frame_index"] = (
                spatial_item.frame_index if spatial_item is not None else None
            )
        batch_state: CandidateBatchState | None = (
            {
                key: (
                    copy.deepcopy(batch.assigned_user_ids)
                    if key == "assigned_user_ids"
                    else self._plain(getattr(batch, key))
                )
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
            else None
        )
        return {
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "dataset_item_id": str(item.id) if item else None,
            "scene_id": (
                str(spatial_item.scene_id)
                if spatial_item is not None and spatial_item.scene_id is not None
                else None
            ),
            "source_bucket": self._dataset_bucket if item else self._task_bucket,
            "source_key": task.file_path,
            "file_type": task.file_type,
            "expected_width": item.width if item else None,
            "expected_height": item.height if item else None,
            "task": task_state,
            "item": item_state,
            "batch": batch_state,
        }

    async def list_candidates(self, request: CandidatePageRead) -> CandidatePoolPage:
        total = await self._db.scalar(
            select(func.count()).select_from(
                self._candidate_query(request.project_id).subquery()
            )
        )
        rows = (
            await self._db.execute(
                self._candidate_query(request.project_id)
                .order_by(Task.created_at, Task.id)
                .limit(request.limit)
                .offset(request.offset)
            )
        ).all()
        spatial_items, _ = await read_spatial_item_facts(
            self._db, [item.id for _, item, _ in rows if item is not None]
        )
        items: list[CandidatePoolItem] = [
            {
                "task_id": str(task.id),
                "display_id": task.display_id,
                "file_name": task.file_name,
                "dataset_item_id": str(item.id) if item else None,
                "scene_id": (
                    str(spatial_items[item.id].scene_id)
                    if item is not None
                    and item.id in spatial_items
                    and spatial_items[item.id].scene_id is not None
                    else None
                ),
            }
            for task, item, _batch in rows
        ]
        return {
            "total": total,
            "limit": request.limit,
            "offset": request.offset,
            "items": items,
        }

    async def read_candidate_sources(
        self, request: CandidateSourcesRead
    ) -> list[CandidateSourceSnapshot]:
        rows = (
            await self._db.execute(
                self._candidate_query(request.project_id)
                .where(Task.id.in_(request.task_ids))
                .order_by(Task.id)
            )
        ).all()
        if len(rows) != len(request.task_ids):
            raise CandidateSourceError(
                "pool_stale", "候选须为未分派、无标注或草稿且未被编辑锁定的图片任务"
            )
        spatial_items, _ = await read_spatial_item_facts(
            self._db, [item.id for _, item, _ in rows if item is not None]
        )
        result = [
            self._source_snapshot(
                *row,
                spatial_items.get(row[1].id) if row[1] is not None else None,
            )
            for row in rows
        ]
        if any(
            row["item"]
            and (
                row["item"]["file_path"] != row["source_key"]
                or row["item"]["file_type"] != "image"
            )
            for row in result
        ):
            raise CandidateSourceError("source_changed", "任务与素材图片引用不一致")
        return result

    async def lock_candidate_edits(self, request: CandidateEditFence) -> None:
        locks = TaskLockService(self._db)
        for task_id in sorted(request.task_ids):
            acquired = await self._db.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        func.hashtextextended(f"aap:task-edit-lock:{task_id}", 0)
                    )
                )
            )
            if not acquired:
                raise CandidateSourceError(
                    "pool_busy", "候选编辑锁正在变化，请稍后重新检查"
                )
            try:
                await locks.assert_write_allowed(task_id, request.actor_id)
            except TaskLockConflictError as exc:
                raise CandidateSourceError(
                    "pool_locked", "候选正在编辑，请关闭工作台后重新选样"
                ) from exc
            if await locks.active_lock(task_id) is not None:
                raise CandidateSourceError(
                    "pool_locked", "候选正在编辑；自己的工作台锁也须先释放"
                )
