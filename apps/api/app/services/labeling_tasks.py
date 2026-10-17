from __future__ import annotations

import uuid

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import FromClause

from app.db.models.project import Project
from app.db.models.task import Task
from app.services import async_job as async_job_svc
from app.services.dataset_task_facts import (
    PreparedDatasetItem,
    PreparedPointCloudFrame,
)
from app.services.task_dataset_link import link_items


class LabelingTaskService:
    """Own task writes, task-linked media, and labeling progress projections."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_initial_generic(
        self,
        *,
        project_id: uuid.UUID,
        pending_items: tuple[PreparedDatasetItem, ...],
        job_id: uuid.UUID | None = None,
        chunk_size: int = 5000,
    ) -> dict[str, int]:
        """Insert the workflow's pending snapshot without a second preselection."""
        total = len(pending_items)
        if total == 0:
            return {"created": 0, "total": 0}

        created = 0
        last_pct = 0
        for start in range(0, total, chunk_size):
            chunk = pending_items[start : start + chunk_size]
            seq_result = await self.db.execute(
                text("SELECT nextval('display_seq_tasks') FROM generate_series(1, :n)"),
                {"n": len(chunk)},
            )
            display_nums = [row[0] for row in seq_result.all()]
            rows = [
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "dataset_item_id": item.id,
                    "batch_id": None,
                    "display_id": f"T-{display_nums[index]}",
                    "file_name": item.file_name,
                    "file_path": item.file_path,
                    "file_type": item.file_type,
                    "status": "pending",
                    "annotation_contributor_ids": [],
                }
                for index, item in enumerate(chunk)
            ]
            await self.db.execute(insert(Task), rows)
            created += len(chunk)

            project = await self.db.get(Project, project_id)
            if project:
                project.total_tasks = (project.total_tasks or 0) + len(chunk)

            if job_id is not None:
                pct = int(created / total * 100)
                if pct >= last_pct + 5 or created == total:
                    await async_job_svc.update_progress(self.db, job_id, pct)
                    last_pct = pct

            await self.db.commit()

        return {"created": created, "total": total}

    async def create_initial_pointcloud(
        self,
        *,
        project_id: uuid.UUID,
        frames: tuple[PreparedPointCloudFrame, ...],
        job_id: uuid.UUID | None = None,
        chunk_size: int = 5000,
    ) -> dict[str, int]:
        if not frames:
            return {"created": 0, "total": 0}

        lidar_ids = [frame.lidar.id for frame in frames]
        existing_rows = await self.db.execute(
            select(Task.dataset_item_id).where(
                Task.project_id == project_id,
                Task.dataset_item_id.in_(lidar_ids),
            )
        )
        existing_ids = {row[0] for row in existing_rows.all()}
        pending = [frame for frame in frames if frame.lidar.id not in existing_ids]
        total = len(pending)
        if total == 0:
            return {"created": 0, "total": 0}

        created = 0
        last_pct = 0
        for start in range(0, total, chunk_size):
            chunk = pending[start : start + chunk_size]
            seq_result = await self.db.execute(
                text("SELECT nextval('display_seq_tasks') FROM generate_series(1, :n)"),
                {"n": len(chunk)},
            )
            display_nums = [row[0] for row in seq_result.all()]

            for index, frame in enumerate(chunk):
                lidar = frame.lidar
                task = Task(
                    project_id=project_id,
                    dataset_item_id=lidar.id,
                    batch_id=None,
                    display_id=f"T-{display_nums[index]}",
                    file_name=lidar.file_name,
                    file_path=lidar.file_path,
                    file_type="point_cloud",
                    status="pending",
                    annotation_contributor_ids=[],
                )
                self.db.add(task)
                await self.db.flush()
                link_payload: list[tuple[uuid.UUID, str, str | None]] = [
                    (lidar.id, "primary_lidar", None)
                ]
                link_payload.extend(
                    (camera_item.id, f"camera_{camera}", camera)
                    for camera, camera_item in frame.cameras
                )
                await link_items(self.db, task.id, link_payload)

            created += len(chunk)
            project = await self.db.get(Project, project_id)
            if project:
                project.total_tasks = (project.total_tasks or 0) + len(chunk)

            if job_id is not None:
                pct = int(created / total * 100)
                if pct >= last_pct + 5 or created == total:
                    await async_job_svc.update_progress(self.db, job_id, pct)
                    last_pct = pct

            await self.db.commit()

        return {"created": created, "total": total}

    async def append_items(
        self,
        *,
        project_ids: tuple[uuid.UUID, ...],
        items: tuple[PreparedDatasetItem, ...],
    ) -> int:
        if not project_ids or not items:
            return 0

        existing_rows = await self.db.execute(
            select(Task.project_id, Task.dataset_item_id).where(
                Task.project_id.in_(project_ids),
                Task.dataset_item_id.in_([item.id for item in items]),
            )
        )
        existing_pairs = {(row[0], row[1]) for row in existing_rows.all()}
        pending = [
            (project_id, item)
            for project_id in project_ids
            for item in items
            if (project_id, item.id) not in existing_pairs
        ]
        if not pending:
            return 0

        total_created = 0
        chunk_size = 5000
        for start in range(0, len(pending), chunk_size):
            chunk = pending[start : start + chunk_size]
            seq_result = await self.db.execute(
                text("SELECT nextval('display_seq_tasks') FROM generate_series(1, :n)"),
                {"n": len(chunk)},
            )
            display_nums = [row[0] for row in seq_result.all()]
            rows = [
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "dataset_item_id": item.id,
                    "batch_id": None,
                    "display_id": f"T-{display_nums[index]}",
                    "file_name": item.file_name,
                    "file_path": item.file_path,
                    "file_type": item.file_type,
                    "status": "pending",
                    "annotation_contributor_ids": [],
                }
                for index, (project_id, item) in enumerate(chunk)
            ]
            await self.db.execute(insert(Task), rows)
            total_created += len(chunk)

        created_by_project: dict[uuid.UUID, int] = {}
        for project_id, _item in pending:
            created_by_project[project_id] = created_by_project.get(project_id, 0) + 1
        projects = (
            (await self.db.execute(select(Project).where(Project.id.in_(project_ids))))
            .scalars()
            .all()
        )
        for project in projects:
            project.total_tasks = (project.total_tasks or 0) + created_by_project.get(
                project.id, 0
            )

        await self.db.flush()
        return total_created

    async def delete_unlinked_tasks(self, task_ids: list[uuid.UUID]) -> dict[str, int]:
        """Delete the limited legacy child set used by dataset unlink."""
        if not task_ids:
            return {"deleted_tasks": 0, "deleted_annotations": 0}

        from app.db.models.annotation import Annotation
        from app.db.models.annotation_comment import AnnotationComment
        from app.db.models.task_lock import TaskLock

        annotation_ids = list(
            (
                await self.db.execute(
                    select(Annotation.id).where(Annotation.task_id.in_(task_ids))
                )
            )
            .scalars()
            .all()
        )
        if annotation_ids:
            await self.db.execute(
                delete(AnnotationComment).where(
                    AnnotationComment.annotation_id.in_(annotation_ids)
                )
            )
        await self.db.execute(
            delete(Annotation).where(Annotation.task_id.in_(task_ids))
        )
        await self.db.execute(delete(TaskLock).where(TaskLock.task_id.in_(task_ids)))
        await self.db.execute(delete(Task).where(Task.id.in_(task_ids)))
        return {
            "deleted_tasks": len(task_ids),
            "deleted_annotations": len(annotation_ids),
        }

    async def recalculate_unlink_counters(
        self, project_id: uuid.UUID, affected_batch_ids: set[uuid.UUID]
    ) -> list[uuid.UUID]:
        from app.db.models.task_batch import TaskBatch

        project = await self.db.get(Project, project_id)
        if project:
            row = (
                await self.db.execute(
                    select(
                        func.count().label("total"),
                        func.count()
                        .filter(Task.status == "completed")
                        .label("completed"),
                        func.count().filter(Task.status == "review").label("review"),
                    ).where(Task.project_id == project_id)
                )
            ).one()
            project.total_tasks = row.total
            project.completed_tasks = row.completed
            project.review_tasks = row.review

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

        deleted_batch_ids: list[uuid.UUID] = []
        if affected_batch_ids:
            for batch in batches:
                if (
                    batch.id in affected_batch_ids
                    and batch.total_tasks == 0
                    and batch.display_id != "B-DEFAULT"
                ):
                    await self.db.execute(
                        delete(TaskBatch).where(TaskBatch.id == batch.id)
                    )
                    deleted_batch_ids.append(batch.id)
        return deleted_batch_ids

    async def delete_project_annotation_comments(self, project_id: uuid.UUID) -> None:
        await self.db.execute(
            text("DELETE FROM annotation_comments WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )

    async def delete_project_annotation_drafts(self, project_id: uuid.UUID) -> None:
        await self.db.execute(
            text(
                "DELETE FROM annotation_drafts WHERE task_id IN "
                "(SELECT id FROM tasks WHERE project_id = :pid)"
            ),
            {"pid": str(project_id)},
        )

    async def delete_project_prediction_metadata(self, project_id: uuid.UUID) -> None:
        await self.db.execute(
            text(
                "DELETE FROM prediction_metas WHERE prediction_id IN "
                "(SELECT id FROM predictions WHERE project_id = :pid) "
                "OR failed_prediction_id IN "
                "(SELECT id FROM failed_predictions WHERE project_id = :pid)"
            ),
            {"pid": str(project_id)},
        )

    async def clear_project_annotation_parents(self, project_id: uuid.UUID) -> None:
        await self.db.execute(
            text(
                "UPDATE annotations SET parent_prediction_id = NULL, "
                "parent_annotation_id = NULL WHERE project_id = :pid"
            ),
            {"pid": str(project_id)},
        )

    async def delete_project_annotations_and_predictions(
        self, project_id: uuid.UUID
    ) -> None:
        params = {"pid": str(project_id)}
        await self.db.execute(
            text("DELETE FROM annotations WHERE project_id = :pid"), params
        )
        await self.db.execute(
            text("DELETE FROM predictions WHERE project_id = :pid"), params
        )
        await self.db.execute(
            text("DELETE FROM failed_predictions WHERE project_id = :pid"), params
        )

    async def delete_project_task_locks(self, project_id: uuid.UUID) -> None:
        await self.db.execute(
            text(
                "DELETE FROM task_locks WHERE task_id IN "
                "(SELECT id FROM tasks WHERE project_id = :pid)"
            ),
            {"pid": str(project_id)},
        )

    async def delete_project_tasks(self, project_id: uuid.UUID) -> None:
        await self.db.execute(
            text("DELETE FROM tasks WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )

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

        project = await self.db.get(Project, project_id)
        if project:
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
            project.total_tasks = row.total
            project.completed_tasks = row.completed
            project.review_tasks = row.review
            project.in_progress_tasks = row.in_progress

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
