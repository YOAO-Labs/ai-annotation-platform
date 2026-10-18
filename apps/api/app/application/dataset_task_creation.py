from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dataset import DatasetService
from app.services.labeling_task_creation_contract import (
    LabelingTaskCreationPort,
    TaskCreationCounts,
)
from app.services.labeling_tasks import LabelingTaskService
from app.services.pointcloud_import import attach_calibration, prepare_pointcloud_frames
from app.services.project_access import lock_creation_project_manager
from app.services.system_settings_service import SystemSettingsService
from app.services.task_creation_contract import (
    CreationMode,
    CreationOperation,
    TaskCreationFailure,
)
from app.services.task_creation_jobs import TaskCreationJobs


@dataclass(frozen=True)
class LinkProjectResult:
    link_id: uuid.UUID
    async_job_id: uuid.UUID | None = None
    created_tasks: int = 0


class DatasetTaskCreationWorkflow:
    """Compose owner capabilities while retaining the caller's commit policy."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.assets = DatasetService(db)
        self.labeling: LabelingTaskCreationPort = LabelingTaskService(db)
        self.jobs = TaskCreationJobs(db)

    async def link_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> LinkProjectResult:
        association = await self.assets.associate_project(
            dataset_id, project_id, actor_id=actor_id
        )
        if association.existing:
            job = await self.jobs.find(
                actor_id=actor_id,
                dataset_id=dataset_id,
                project_id=project_id,
                association_id=association.link_id,
            )
            if job.job_id is not None:
                return LinkProjectResult(
                    link_id=association.link_id, async_job_id=job.job_id
                )
            try:
                operation = await self.labeling.read_dataset_task_creation(
                    association.link_id
                )
            except TaskCreationFailure as exc:
                if exc.code != "operation_missing":
                    raise
                return LinkProjectResult(link_id=association.link_id)
            if (
                operation.dataset_id != dataset_id
                or operation.project_id != project_id
                or operation.association_id != association.link_id
                or operation.job_id is not None
            ):
                raise ValueError("Linked creation does not match its association")
            if operation.state == "completed":
                return LinkProjectResult(link_id=association.link_id)
            result = await self._run_operation(operation)
            return LinkProjectResult(
                link_id=association.link_id, created_tasks=result["created"]
            )

        mode: CreationMode = (
            "pointcloud" if association.project_data_type == "lidar" else "generic"
        )
        sync_threshold = await SystemSettingsService.get(
            self.db, "task_create_sync_threshold"
        )
        # The association identifies a synchronous operation across a pre-task commit.
        operation_id = association.link_id
        job_id = None
        if association.item_count > sync_threshold:
            operation_id = uuid.uuid4()
            job = await self.jobs.create(
                operation_id=operation_id,
                actor_id=actor_id,
                dataset_id=dataset_id,
                project_id=project_id,
                association_id=association.link_id,
            )
            job_id = job.job_id
        operation = await self.labeling.accept_dataset_task_creation(
            operation_id=operation_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            project_id=project_id,
            association_id=association.link_id,
            job_id=job_id,
            mode=mode,
            chunk_size=5000,
        )
        if job_id is not None:
            return LinkProjectResult(link_id=association.link_id, async_job_id=job_id)
        result = await self._run_operation(operation)
        return LinkProjectResult(
            link_id=association.link_id, created_tasks=result["created"]
        )

    async def create_tasks_for_linked_dataset(
        self,
        *,
        dataset_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_id: uuid.UUID,
        chunk_size: int = 5000,
        commit_calibration: bool = True,
        commit_task_chunks: bool = True,
    ) -> TaskCreationCounts:
        """Accept a new authorized operation for an existing association."""
        await self.db.flush()
        association_id = await self.assets.lock_project_association(
            dataset_id, project_id
        )
        policy = await lock_creation_project_manager(
            self.db, actor_id=actor_id, project_id=project_id
        )
        if association_id is None:
            raise TaskCreationFailure("association_changed")
        operation = await self.labeling.accept_dataset_task_creation(
            operation_id=uuid.uuid4(),
            actor_id=actor_id,
            dataset_id=dataset_id,
            project_id=project_id,
            association_id=association_id,
            job_id=None,
            mode="pointcloud" if policy.data_type == "lidar" else "generic",
            chunk_size=chunk_size,
        )
        return await self._run_operation(
            operation,
            commit_calibration=commit_calibration,
            commit_task_chunks=commit_task_chunks,
        )

    async def resume_creation(
        self,
        operation_id: uuid.UUID,
        *,
        commit_calibration: bool = True,
        commit_task_chunks: bool = True,
    ) -> TaskCreationCounts:
        operation = await self.labeling.read_dataset_task_creation(operation_id)
        return await self._run_operation(
            operation,
            commit_calibration=commit_calibration,
            commit_task_chunks=commit_task_chunks,
        )

    async def _run_operation(
        self,
        operation: CreationOperation,
        *,
        commit_calibration: bool = True,
        commit_task_chunks: bool = True,
    ) -> TaskCreationCounts:
        if operation.state == "accepted":
            frames = None
            if operation.mode == "pointcloud":
                await attach_calibration(self.db, dataset_id=operation.dataset_id)
                if commit_calibration:
                    # Calibration retains its pre-task commit, including replays.
                    await self.db.commit()
                    operation = await self.labeling.read_dataset_task_creation(
                        operation.operation_id
                    )
                prepared = await prepare_pointcloud_frames(
                    self.db, dataset_id=operation.dataset_id
                )
                frames = [
                    {
                        "primary_item_id": str(frame.lidar.id),
                        "cameras": [
                            {"item_id": str(item.id), "sensor_name": camera}
                            for camera, item in frame.cameras
                        ],
                    }
                    for frame in prepared
                ]
            operation = await self.labeling.prepare_dataset_task_creation(
                operation_id=operation.operation_id, frames=frames
            )

        while operation.state != "completed":
            chunk = await self.labeling.advance_dataset_task_creation(
                operation.operation_id, operation.next_chunk
            )
            if chunk.project_id != operation.project_id:
                raise ValueError("Creation chunk belongs to another project")
            if commit_task_chunks:
                await self.db.commit()
            operation = operation.model_copy(
                update={
                    "created": chunk.cumulative_created,
                    "processed": chunk.cumulative_processed,
                    "next_chunk": chunk.next_chunk,
                    "state": "completed" if chunk.complete else "running",
                }
            )
        assert operation.total is not None
        return {"created": operation.created, "total": operation.total}

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
