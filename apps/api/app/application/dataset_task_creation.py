from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJobKind
from app.db.models.project import Project
from app.services import async_job as async_job_svc
from app.services.dataset import DatasetService
from app.services.labeling_task_creation_contract import (
    LabelingTaskCreationPort,
    TaskCreationCounts,
)
from app.services.labeling_tasks import LabelingTaskService
from app.services.pointcloud_import import (
    attach_calibration,
    prepare_pointcloud_frames,
)
from app.services.system_settings_service import SystemSettingsService


@dataclass(frozen=True)
class LinkProjectResult:
    link_id: uuid.UUID
    async_job_id: uuid.UUID | None = None
    created_tasks: int = 0


class DatasetTaskCreationWorkflow:
    """Compose dataset association and asset facts with labeling task writes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.assets = DatasetService(db)
        self.labeling: LabelingTaskCreationPort = LabelingTaskService(db)

    async def link_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> LinkProjectResult:
        association = await self.assets.associate_project(dataset_id, project_id)
        if association.existing:
            return LinkProjectResult(link_id=association.link_id)

        # Freeze the synchronous/queued choice at request time. A queued job
        # keeps the link branch even if the setting changes before execution.
        sync_threshold = await SystemSettingsService.get(
            self.db, "task_create_sync_threshold"
        )
        if association.item_count > sync_threshold:
            job = await async_job_svc.create_job(
                self.db,
                kind=AsyncJobKind.CREATE_TASKS.value,
                project_id=project_id,
                payload={
                    "dataset_id": str(dataset_id),
                    "project_id": str(project_id),
                    "mode": "link",
                },
            )
            return LinkProjectResult(link_id=association.link_id, async_job_id=job.id)

        result = await self.create_tasks_for_linked_dataset(
            dataset_id=dataset_id, project_id=project_id
        )
        return LinkProjectResult(
            link_id=association.link_id, created_tasks=result["created"]
        )

    async def create_tasks_for_linked_dataset(
        self,
        *,
        dataset_id: uuid.UUID,
        project_id: uuid.UUID,
        job_id: uuid.UUID | None = None,
        chunk_size: int = 5000,
        commit_calibration: bool = True,
        commit_task_chunks: bool = True,
    ) -> TaskCreationCounts:
        """Create missing tasks, optionally retaining the caller's SQL transaction."""
        project = await self.db.get(Project, project_id)
        if project is not None and project.data_type == "lidar":
            await attach_calibration(self.db, dataset_id=dataset_id)
            if commit_calibration:
                # Calibration is committed before scene inference, including replays
                # where every lidar frame already has a task.
                await self.db.commit()
            frames = await prepare_pointcloud_frames(self.db, dataset_id=dataset_id)
            return await self.labeling.create_initial_pointcloud(
                project_id=project_id,
                frames=frames,
                job_id=job_id,
                chunk_size=chunk_size,
                commit_each_chunk=commit_task_chunks,
            )
        pending_items = await self.labeling.read_pending_dataset_items(
            dataset_id=dataset_id,
            project_id=project_id,
        )
        return await self.labeling.create_initial_generic(
            project_id=project_id,
            pending_items=pending_items,
            job_id=job_id,
            chunk_size=chunk_size,
            commit_each_chunk=commit_task_chunks,
        )

    async def append_items(
        self, dataset_id: uuid.UUID, item_ids: list[uuid.UUID]
    ) -> int:
        unique_item_ids = tuple(dict.fromkeys(item_ids))
        if not unique_item_ids:
            return 0
        return await self.labeling.append_dataset_items(
            dataset_id=dataset_id,
            item_ids=unique_item_ids,
            operation_id=uuid.uuid4(),
        )
