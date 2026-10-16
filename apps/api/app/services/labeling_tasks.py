from __future__ import annotations

import uuid

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.task import Task
from app.services import async_job as async_job_svc
from app.services.dataset_task_facts import (
    PreparedDatasetItem,
    PreparedPointCloudFrame,
)
from app.services.task_dataset_link import link_items


class LabelingTaskService:
    """Own task-creation writes, media links, progress and project-total projections."""

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
