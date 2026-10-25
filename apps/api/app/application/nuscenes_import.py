from __future__ import annotations

import uuid

from aap_assets_source.operator_contract import RecountOperatorItemsRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.services.dataset import DatasetService
from app.services.assets_operator import AssetsOperatorGateway
from app.services.project_workspace import create_import_project, find_import_project


class NuScenesImportWorkflow:
    """Compose nuScenes project, association, calibration, and task writes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_project_and_tasks(
        self,
        *,
        dataset_id: uuid.UUID,
        project_display_id: str,
        project_name: str,
        owner_id: uuid.UUID,
        tool_bindings: dict | None,
    ) -> uuid.UUID:
        project_id = await find_import_project(self.db, project_display_id)
        if project_id is None:
            project_id = await create_import_project(
                self.db,
                display_id=project_display_id,
                name=project_name,
                owner_id=owner_id,
                tool_bindings=tool_bindings,
            )

        assets = DatasetService(self.db)
        await assets.associate_project(dataset_id, project_id, actor_id=owner_id)
        await DatasetTaskCreationWorkflow(self.db).create_tasks_for_linked_dataset(
            dataset_id=dataset_id,
            project_id=project_id,
            actor_id=owner_id,
            commit_calibration=False,
            commit_task_chunks=False,
        )
        await assets.record_nuscenes_calibration_digests(dataset_id)
        await AssetsOperatorGateway(self.db).recount_operator_items(
            RecountOperatorItemsRequest(
                operation_id=uuid.uuid4(), dataset_id=dataset_id
            )
        )
        return project_id
