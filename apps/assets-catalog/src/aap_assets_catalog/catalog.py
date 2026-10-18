"""Assets catalog use cases bound to native SQL and a read-only URL signer."""

from __future__ import annotations

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


class CatalogService:
    def __init__(self, database: CatalogDatabase, signer: CatalogObjectSigner):
        self._database = database
        self._signer = signer

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
            return cls(CatalogService(database, signer), database)
        except Exception:
            await database.close()
            raise

    async def ready(self) -> bool:
        await self._database.ping()
        return True

    async def close(self) -> None:
        await self._database.close()
