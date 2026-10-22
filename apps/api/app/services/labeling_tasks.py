from __future__ import annotations

import uuid
from dataclasses import dataclass
import json
from datetime import datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.selectable import FromClause

from app.db.models.project import Project
from app.db.models.task import Task
from app.services import task_access
from app.services.annotation_evidence import ensure_review_round, start_review_round
from app.services.labeling_project_counter_data import (
    clear_project_counter_projection,
    register_project_counter_projection,
)
from app.services.labeling_task_creation_contract import (
    LabelingTaskCreationPort,
    TaskAppendResult,
)

from app.services.display_id import next_display_id
from app.services.project_access import get_workspace_project
from app.services.task_lock import TaskLockService
from app.services.task_creation_contract import (
    CreationChunk,
    CreationMode,
    CreationOperation,
    decode_creation_reply,
)


class LabelingUploadTaskNotFound(LookupError):
    pass


class LabelingUploadTaskNotUploading(ValueError):
    pass


class LabelingUploadTargetChanged(ValueError):
    pass


@dataclass(frozen=True)
class LabelingUploadTarget:
    task_id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    file_path: str
    file_type: str
    status: str


class LabelingTaskService(LabelingTaskCreationPort):
    """Own task writes, task-linked media, and labeling progress projections."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def clear_dataset_item_sources(self) -> None:
        await self.db.execute(
            text(
                "UPDATE tasks SET dataset_item_id = NULL "
                "WHERE dataset_item_id IS NOT NULL"
            )
        )

    async def _submission_assignment_start(
        self, task_id: uuid.UUID, user_id: uuid.UUID, now: datetime
    ) -> datetime:
        """Preserve the actor's active work start before releasing its lock."""
        from app.db.models.task_lock import TaskLock

        started_at = await self.db.scalar(
            select(TaskLock.created_at).where(
                TaskLock.task_id == task_id,
                TaskLock.user_id == user_id,
                TaskLock.expire_at > now,
                TaskLock.created_at <= now,
            )
        )
        return started_at or now

    async def submit_task(
        self, task: Task, *, actor_id: uuid.UUID, now: datetime
    ) -> uuid.UUID:
        if task.assignee_id is None:
            task.assignee_id = (
                await task_access.resolve_effective_task_assignee_id(self.db, task)
                or actor_id
            )
            task.assigned_at = await self._submission_assignment_start(
                task.id, task.assignee_id, now
            )

        review_round_id = start_review_round(task)
        task.status = "review"
        task.submitted_at = now
        task.reviewer_id = None
        task.reviewer_is_override = False
        task.reviewer_claimed_at = None
        task.reviewed_at = None
        task.reject_reason = None
        task.reject_reason_type = None
        await TaskLockService(self.db).release(task.id, actor_id)
        return review_round_id

    async def skip_task(
        self, task: Task, *, reason: str, actor_id: uuid.UUID, now: datetime
    ) -> tuple[uuid.UUID, uuid.UUID | None]:
        inherited_assignee_id = await task_access.resolve_effective_task_assignee_id(
            self.db, task
        )
        if task.assignee_id is None:
            task.assignee_id = actor_id
            task.assigned_at = await self._submission_assignment_start(
                task.id, actor_id, now
            )

        review_round_id = start_review_round(task)
        task.status = "review"
        task.skip_reason = reason
        task.skipped_at = now
        task.submitted_at = now
        task.reviewer_id = None
        task.reviewer_is_override = False
        task.reviewer_claimed_at = None
        task.reviewed_at = None
        task.reject_reason = None
        task.reject_reason_type = None
        await TaskLockService(self.db).release(task.id, actor_id)
        return review_round_id, inherited_assignee_id

    @staticmethod
    def withdraw_task(task: Task) -> None:
        task.status = "in_progress"
        task.submitted_at = None

    @staticmethod
    def reopen_task(task: Task, *, now: datetime) -> uuid.UUID | None:
        original_reviewer_id = task.reviewer_id
        task.status = "in_progress"
        task.reopened_count = (task.reopened_count or 0) + 1
        task.last_reopened_at = now
        task.reviewer_id = None
        task.reviewer_is_override = False
        task.reviewer_claimed_at = None
        task.reviewed_at = None
        task.reject_reason = None
        task.reject_reason_type = None
        task.submitted_at = None
        return original_reviewer_id

    @staticmethod
    def accept_rejection(task: Task) -> None:
        task.status = "in_progress"

    @staticmethod
    def claim_review(
        task: Task, *, reviewer_id: uuid.UUID, claimed_at: datetime
    ) -> uuid.UUID:
        task.reviewer_id = reviewer_id
        task.reviewer_claimed_at = claimed_at
        return ensure_review_round(task)

    @staticmethod
    def approve_review(
        task: Task, *, reviewer_id: uuid.UUID, reviewed_at: datetime
    ) -> uuid.UUID:
        task.status = "completed"
        task.reviewed_at = reviewed_at
        if task.reviewer_id is None:
            task.reviewer_id = reviewer_id
        if task.reviewer_claimed_at is None:
            task.reviewer_claimed_at = reviewed_at
        return ensure_review_round(task)

    @staticmethod
    def reject_review(
        task: Task,
        *,
        reviewer_id: uuid.UUID,
        reviewed_at: datetime,
        reason_type: str,
        reason: str | None,
    ) -> uuid.UUID:
        task.status = "rejected"
        task.reviewed_at = reviewed_at
        task.reject_reason_type = reason_type
        task.reject_reason = reason
        if task.reviewer_id is None:
            task.reviewer_id = reviewer_id
        if task.reviewer_claimed_at is None:
            task.reviewer_claimed_at = reviewed_at
        return ensure_review_round(task)

    async def decrement_project_review_count(self, project: Project) -> None:
        count = await self.db.scalar(
            select(Project.review_tasks).from_statement(
                text(
                    "SELECT aap_labeling_v1.decrement_project_review_count("
                    "CAST(:project_id AS uuid)) AS review_tasks"
                )
            ),
            {"project_id": project.id},
        )
        set_committed_value(project, "review_tasks", count)

    async def decrement_project_completed_count(self, project: Project) -> None:
        count = await self.db.scalar(
            select(Project.completed_tasks).from_statement(
                text(
                    "SELECT aap_labeling_v1.decrement_project_completed_count("
                    "CAST(:project_id AS uuid)) AS completed_tasks"
                )
            ),
            {"project_id": project.id},
        )
        set_committed_value(project, "completed_tasks", count)

    async def record_project_review_approval(self, project: Project) -> None:
        counts = (
            await self.db.execute(
                select(Project.completed_tasks, Project.review_tasks).from_statement(
                    text(
                        "SELECT completed_tasks, review_tasks "
                        "FROM aap_labeling_v1.record_project_review_approval("
                        "CAST(:project_id AS uuid))"
                    ),
                ),
                {"project_id": project.id},
            )
        ).one()
        set_committed_value(project, "completed_tasks", counts.completed_tasks)
        set_committed_value(project, "review_tasks", counts.review_tasks)

    async def accept_dataset_task_creation(
        self,
        *,
        operation_id: uuid.UUID,
        actor_id: uuid.UUID,
        dataset_id: uuid.UUID,
        project_id: uuid.UUID,
        association_id: uuid.UUID,
        job_id: uuid.UUID | None,
        mode: CreationMode,
        chunk_size: int,
    ) -> CreationOperation:
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT aap_labeling_v1.accept_dataset_task_creation("
                "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
                "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
                "CAST(:association_id AS uuid), CAST(:job_id AS uuid), "
                ":mode, :chunk_size)"
            ),
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project_id,
                "association_id": association_id,
                "job_id": job_id,
                "mode": mode,
                "chunk_size": chunk_size,
            },
        )
        result = decode_creation_reply(
            payload, "aap-task-creation/v1", CreationOperation
        )
        if (
            result.operation_id != operation_id
            or result.actor_id != actor_id
            or result.dataset_id != dataset_id
            or result.project_id != project_id
            or result.association_id != association_id
            or result.job_id != job_id
            or result.mode != mode
            or result.chunk_size != chunk_size
        ):
            raise ValueError("Accepted creation does not match its request")
        return result

    async def prepare_dataset_task_creation(
        self,
        *,
        operation_id: uuid.UUID,
        frames: list[dict[str, object]] | None,
    ) -> CreationOperation:
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT aap_labeling_v1.prepare_dataset_task_creation("
                "CAST(:operation_id AS uuid), CAST(:frames AS jsonb))"
            ),
            {
                "operation_id": operation_id,
                "frames": json.dumps(frames) if frames is not None else None,
            },
        )
        result = decode_creation_reply(
            payload, "aap-task-creation/v1", CreationOperation
        )
        if result.operation_id != operation_id:
            raise ValueError("Prepared creation does not match its request")
        return result

    async def read_dataset_task_creation(
        self, operation_id: uuid.UUID
    ) -> CreationOperation:
        payload = await self.db.scalar(
            text(
                "SELECT aap_labeling_v1.read_dataset_task_creation("
                "CAST(:operation_id AS uuid))"
            ),
            {"operation_id": operation_id},
        )
        result = decode_creation_reply(
            payload, "aap-task-creation/v1", CreationOperation
        )
        if result.operation_id != operation_id:
            raise ValueError("Creation snapshot does not match its request")
        return result

    async def advance_dataset_task_creation(
        self, operation_id: uuid.UUID, chunk_index: int
    ) -> CreationChunk:
        payload = await self.db.scalar(
            text(
                "SELECT aap_labeling_v1.advance_dataset_task_creation("
                "CAST(:operation_id AS uuid), :chunk_index)"
            ),
            {"operation_id": operation_id, "chunk_index": chunk_index},
        )
        result = decode_creation_reply(
            payload, "aap-task-creation-chunk/v1", CreationChunk
        )
        if result.operation_id != operation_id or result.chunk_index != chunk_index:
            raise ValueError("Creation chunk does not match its request")
        project = self.db.identity_map.get(
            identity_key(Project, ident=result.project_id)
        )
        if project is not None:
            self.db.expire(project, ["total_tasks", "updated_at"])
        return result

    async def append_dataset_items(
        self,
        *,
        dataset_id: uuid.UUID,
        item_ids: tuple[uuid.UUID, ...],
        operation_id: uuid.UUID,
    ) -> int:
        # Flush source inserts into the same transaction before crossing the data API.
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT aap_labeling_v1.append_dataset_items("
                "CAST(:dataset_id AS uuid), CAST(:item_ids AS uuid[]), "
                "CAST(:operation_id AS uuid))"
            ),
            {
                "dataset_id": dataset_id,
                "item_ids": list(item_ids),
                "operation_id": operation_id,
            },
        )
        result = TaskAppendResult.model_validate(payload)
        if result.dataset_id != dataset_id or result.operation_id != operation_id:
            raise ValueError("Task append receipt does not match its request")
        for total in result.project_totals:
            project = self.db.identity_map.get(
                identity_key(Project, ident=total.project_id)
            )
            if project is not None:
                # Replayed receipts contain historical totals, not current projections.
                self.db.expire(project, ["total_tasks", "updated_at"])
        return result.created

    async def delete_source_less_task_children(self, task_ids: FromClause) -> int:
        from app.db.models.annotation import Annotation
        from app.db.models.annotation_comment import AnnotationComment
        from app.db.models.task_lock import AnnotationDraft, TaskLock

        task_id_select = select(task_ids.c.id)
        annotation_ids = select(Annotation.id).where(
            Annotation.task_id.in_(task_id_select)
        )
        annotation_count = (
            await self.db.execute(
                select(func.count(Annotation.id)).where(
                    Annotation.task_id.in_(task_id_select)
                )
            )
        ).scalar() or 0

        await self.db.execute(
            delete(AnnotationComment)
            .where(AnnotationComment.annotation_id.in_(annotation_ids))
            .execution_options(synchronize_session=False)
        )
        await self.db.execute(
            delete(AnnotationDraft)
            .where(AnnotationDraft.task_id.in_(task_id_select))
            .execution_options(synchronize_session=False)
        )
        await self.db.execute(
            update(Annotation)
            .where(Annotation.task_id.in_(task_id_select))
            .values(parent_prediction_id=None, parent_annotation_id=None)
            .execution_options(synchronize_session=False)
        )
        await self.db.execute(
            delete(Annotation)
            .where(Annotation.task_id.in_(task_id_select))
            .execution_options(synchronize_session=False)
        )
        await self.db.execute(
            delete(TaskLock)
            .where(TaskLock.task_id.in_(task_id_select))
            .execution_options(synchronize_session=False)
        )
        return int(annotation_count)

    async def delete_source_less_task_records(self, task_ids: FromClause) -> None:
        await self.db.execute(
            delete(Task)
            .where(Task.id.in_(select(task_ids.c.id)))
            .execution_options(synchronize_session=False)
        )

    async def recalculate_source_less_task_counters(
        self, project_id: uuid.UUID
    ) -> None:
        from app.db.models.task_batch import TaskBatch

        register_project_counter_projection(self.db, project_id)
        project = await get_workspace_project(self.db, project_id=project_id)
        if project:
            register_project_counter_projection(self.db, project_id)
            row = (
                await self.db.execute(
                    select(
                        func.count().label("total"),
                        func.count()
                        .filter(Task.status == "completed")
                        .label("completed"),
                        func.count().filter(Task.status == "review").label("review"),
                        func.count()
                        .filter(Task.status == "in_progress")
                        .label("in_progress"),
                    ).where(Task.project_id == project_id)
                )
            ).one()
            register_project_counter_projection(self.db, project_id)
            project.total_tasks = row.total
            project.completed_tasks = row.completed
            project.review_tasks = row.review
            project.in_progress_tasks = row.in_progress
        else:
            clear_project_counter_projection(self.db, project_id)

        batches = (
            (
                await self.db.execute(
                    select(TaskBatch).where(TaskBatch.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        for batch in batches:
            counts = (
                await self.db.execute(
                    select(
                        func.count().label("total"),
                        func.count()
                        .filter(Task.status == "completed")
                        .label("completed"),
                        func.count().filter(Task.status == "review").label("review"),
                    ).where(Task.batch_id == batch.id)
                )
            ).one()
            batch.total_tasks = counts.total
            batch.completed_tasks = counts.completed
            batch.review_tasks = counts.review

    async def create_uploading_task(
        self,
        *,
        task_id: uuid.UUID,
        project_id: uuid.UUID,
        file_name: str,
        file_path: str,
        file_type: str,
    ) -> None:
        task = Task(
            id=task_id,
            project_id=project_id,
            display_id=await next_display_id(self.db, "tasks"),
            file_name=file_name,
            file_path=file_path,
            file_type=file_type,
            status="uploading",
        )
        self.db.add(task)
        await self.db.commit()

    async def read_upload_target(self, task_id: uuid.UUID) -> LabelingUploadTarget:
        row = (
            await self.db.execute(
                select(
                    Task.id,
                    Task.project_id,
                    Task.batch_id,
                    Task.file_path,
                    Task.file_type,
                    Task.status,
                ).where(Task.id == task_id)
            )
        ).one_or_none()
        if row is None:
            raise LabelingUploadTaskNotFound
        return LabelingUploadTarget(*row)

    async def mark_uploading_task_pending(self, target: LabelingUploadTarget) -> None:
        """Commit after the workflow holds the canonical current task fence."""
        task = await self.db.get(Task, target.task_id)
        if task is None:
            raise LabelingUploadTaskNotFound
        if task.status != "uploading":
            raise LabelingUploadTaskNotUploading
        if (
            task.project_id != target.project_id
            or task.batch_id != target.batch_id
            or task.file_path != target.file_path
            or task.file_type != target.file_type
        ):
            raise LabelingUploadTargetChanged
        task.status = "pending"
        await self.db.commit()

    async def set_task_thumbnail(
        self, task_id: uuid.UUID, thumbnail_path: str, blurhash: str | None
    ) -> None:
        """Set task thumbnail fields within the caller's transaction."""
        task = await self.db.get(Task, task_id)
        if task is None:
            return
        task.thumbnail_path = thumbnail_path
        if blurhash is not None:
            task.blurhash = blurhash
