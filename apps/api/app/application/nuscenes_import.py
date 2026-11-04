from __future__ import annotations

import uuid

from yoao_assets_source.operator_contract import RecountOperatorItemsRequest
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
        require_new: bool = False,
        fixture_run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        if fixture_run_id is not None and not require_new:
            raise ValueError("fixture project must not reuse an existing workspace")
        project_id = (
            None
            if require_new
            else await find_import_project(self.db, project_display_id)
        )
        if project_id is None:
            project_id = await create_import_project(
                self.db,
                display_id=project_display_id,
                name=project_name,
                owner_id=owner_id,
                tool_bindings=tool_bindings,
            )
        if fixture_run_id is not None:
            from app.services import fixture_run_ownership as ownership

            await ownership.reserve(
                self.db,
                fixture_run_id,
                "project",
                project_id,
                logical_key="pointcloud_demo",
                published=True,
            )
            await self.db.commit()

        assets = DatasetService(self.db)
        await assets.associate_project(dataset_id, project_id, actor_id=owner_id)
        await DatasetTaskCreationWorkflow(self.db).create_tasks_for_linked_dataset(
            dataset_id=dataset_id,
            project_id=project_id,
            actor_id=owner_id,
            commit_calibration=fixture_run_id is not None,
            commit_task_chunks=fixture_run_id is not None,
        )
        await assets.record_nuscenes_calibration_digests(dataset_id)
        await AssetsOperatorGateway(self.db).recount_operator_items(
            RecountOperatorItemsRequest(
                operation_id=uuid.uuid4(), dataset_id=dataset_id
            )
        )
        return project_id
