"""Assets catalog use cases bound to native SQL and a read-only URL signer."""

from __future__ import annotations

from pydantic import BaseModel

from .contract import (
    CatalogItem,
    CreateDatasetRequest,
    DatasetMutationResult,
    DatasetPage,
    DatasetResult,
    GetDatasetRequest,
    ItemPage,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
)
from .associations_contract import (
    DatasetProjectsResult,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
    ProjectDatasetsResult,
)
from .config import CatalogConfig
from .database import CatalogDatabase
from .object_signer import CatalogObjectSigner
from . import spatial_contract as spatial
from .spatial_workflow import SpatialWorkflow


class CatalogService:
    def __init__(
        self,
        database: CatalogDatabase,
        signer: CatalogObjectSigner,
        spatial_workflow: SpatialWorkflow | None = None,
    ):
        self._database = database
        self._signer = signer
        self._spatial = spatial_workflow or SpatialWorkflow(
            database, signer.spatial_source_reader
        )

    async def list_datasets(self, request: ListDatasetsRequest) -> DatasetPage:
        return await self._database.list_datasets(request)

    async def get_dataset(self, request: GetDatasetRequest) -> DatasetResult:
        return await self._database.get_dataset(request)

    async def list_items(self, request: ListItemsRequest) -> ItemPage:
        page = await self._database.list_items(request)
        return page.model_copy(
            update={"items": [self._sign_item(item) for item in page.items]}
        )

    async def create_dataset(
        self, request: CreateDatasetRequest
    ) -> DatasetMutationResult:
        return await self._database.create_dataset(request)

    async def update_dataset(
        self, request: UpdateDatasetRequest
    ) -> DatasetMutationResult:
        return await self._database.update_dataset(request)

    async def list_dataset_projects(
        self, request: ListDatasetProjectsRequest
    ) -> DatasetProjectsResult:
        return await self._database.list_dataset_projects(request)

    async def list_project_datasets(
        self, request: ListProjectDatasetsRequest
    ) -> ProjectDatasetsResult:
        return await self._database.list_project_datasets(request)

    async def spatial_native(self, operation: str, request: BaseModel) -> BaseModel:
        return await self._spatial.invoke_native(operation, request)

    async def backfill_scenes(
        self, request: spatial.BackfillScenesRequest
    ) -> spatial.LayoutMutationResult:
        return await self._spatial.backfill_scenes(request)

    async def sniff_axis(
        self, request: spatial.SniffAxisRequest
    ) -> spatial.AxisSniffResult:
        return await self._spatial.sniff_axis(request)

    async def prepare_pointcloud(
        self, request: spatial.PreparePointcloudRequest
    ) -> spatial.PreparationMutationResult:
        return await self._spatial.prepare_pointcloud(request)

    def _sign_item(self, item: CatalogItem) -> CatalogItem:
        try:
            file_url = self._signer.sign_source(item.file_path)
        except Exception:
            file_url = None
        thumbnail_url = None
        if item.thumbnail_path:
            try:
                thumbnail_url = self._signer.sign_thumbnail(item.thumbnail_path)
            except Exception:
                thumbnail_url = None
        return item.model_copy(
            update={"file_url": file_url, "thumbnail_url": thumbnail_url}
        )


class CatalogRuntime:
    def __init__(self, service: CatalogService, database: CatalogDatabase):
        self.catalog = service
        self._database = database

    @classmethod
    async def start(cls, config: CatalogConfig) -> CatalogRuntime:
        database = await CatalogDatabase.connect(config)
        try:
            signer = CatalogObjectSigner(config)
            workflow = SpatialWorkflow(database, signer.spatial_source_reader)
            return cls(CatalogService(database, signer, workflow), database)
        except Exception:
            await database.close()
            raise

    async def ready(self) -> bool:
        await self._database.ping()
        return True

    async def close(self) -> None:
        await self._database.close()
