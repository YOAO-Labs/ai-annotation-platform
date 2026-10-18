from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from aap_assets_catalog.api import create_app
from aap_assets_catalog.config import CatalogConfig
from aap_assets_catalog.associations_contract import (
    ASSOCIATIONS_SCHEMA,
    AssociationFailure,
    DatasetProjectsResult,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
    ProjectDatasetsResult,
)
from aap_assets_catalog import spatial_contract as spatial
from aap_assets_catalog.contract import (
    CATALOG_SCHEMA,
    CatalogFailure,
    CatalogItem,
    CreateDatasetRequest,
    DatasetMutationResult,
    DatasetPage,
    DatasetResult,
    DatasetSummary,
    GetDatasetRequest,
    ItemPage,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
)

_TOKEN = "assets-catalog-internal-gateway-token-2026"


def _config(
    *, max_request_bytes: int = 1048576, max_response_bytes: int = 16777216
) -> CatalogConfig:
    return CatalogConfig(
        database_url="postgresql://catalog:secret@db/aap",
        driver_database_url="postgresql://catalog:secret@db/aap",
        gateway_token=_TOKEN,
        s3_public_endpoint="http://storage.example.test:9000",
        s3_endpoint="http://storage.example.test:9000",
        s3_region="us-east-1",
        s3_access_key="read-only-key",
        s3_secret_key="read-only-secret",
        datasets_bucket="datasets",
        media_cache_bucket="media-cache",
        host="0.0.0.0",
        port=8766,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
        pool_min_size=1,
        pool_max_size=8,
        pool_timeout_seconds=2,
        sql_timeout_seconds=5,
    )


def _summary(actor_id: UUID | None = None) -> DatasetSummary:
    return DatasetSummary(
        id=uuid4(),
        display_id="D-1",
        name="catalog",
        description="",
        data_type="image",
        is_temporal=False,
        has_scenes=False,
        file_count=0,
        total_size=0,
        created_by=actor_id or uuid4(),
        project_count=0,
        axis_convention=None,
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
        updated_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


class _Catalog:
    def __init__(self):
        self.calls = []
        self.spatial_calls = []
        self.spatial_fail_code = None
        self.fail_code = None
        self.association_fail_code = None
        self.error_message = None
        self.large_metadata = False

    def _raise_if_configured(self):
        if self.fail_code:
            raise CatalogFailure(self.fail_code)
        if self.error_message:
            raise RuntimeError(self.error_message)

    def _raise_association_if_configured(self):
        if self.association_fail_code:
            raise AssociationFailure(self.association_fail_code)
        if self.error_message:
            raise RuntimeError(self.error_message)

    async def list_datasets(self, request: ListDatasetsRequest) -> DatasetPage:
        self.calls.append(("list", request))
        self._raise_if_configured()
        return DatasetPage(
            items=[], total=0, limit=request.limit, offset=request.offset
        )

    async def get_dataset(self, request: GetDatasetRequest) -> DatasetResult:
        self.calls.append(("get", request))
        self._raise_if_configured()
        return DatasetResult(dataset=_summary())

    async def list_items(self, request: ListItemsRequest) -> ItemPage:
        self.calls.append(("items", request))
        self._raise_if_configured()
        return ItemPage(
            dataset_id=request.dataset_id,
            items=[
                CatalogItem(
                    id=uuid4(),
                    dataset_id=request.dataset_id,
                    file_name="frame.jpg",
                    file_path="legacy/frame.jpg",
                    file_type="image",
                    file_size=42,
                    content_hash=None,
                    width=10,
                    height=20,
                    metadata={
                        "unmodeled": {"kept": True},
                        **({"large": "x" * 1024} if self.large_metadata else {}),
                    },
                    file_url="https://storage.test/source",
                    thumbnail_url=None,
                    blurhash=None,
                    created_at=datetime(2026, 10, 18, tzinfo=UTC),
                    thumbnail_path="thumbnails/item/preview.webp",
                )
            ],
            total=1,
            limit=request.limit,
            offset=request.offset,
        )

    async def create_dataset(
        self, request: CreateDatasetRequest
    ) -> DatasetMutationResult:
        self.calls.append(("create", request))
        self._raise_if_configured()
        return DatasetMutationResult(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            dataset=_summary(request.actor_id),
        )

    async def update_dataset(
        self, request: UpdateDatasetRequest
    ) -> DatasetMutationResult:
        self.calls.append(("update", request))
        self._raise_if_configured()
        return DatasetMutationResult(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            dataset=_summary(request.actor_id),
        )

    async def list_dataset_projects(
        self, request: ListDatasetProjectsRequest
    ) -> DatasetProjectsResult:
        self.calls.append(("dataset-projects", request))
        self._raise_association_if_configured()
        return DatasetProjectsResult(dataset_id=request.dataset_id, projects=[])

    async def list_project_datasets(
        self, request: ListProjectDatasetsRequest
    ) -> ProjectDatasetsResult:
        self.calls.append(("project-datasets", request))
        self._raise_association_if_configured()
        return ProjectDatasetsResult(project_id=request.project_id, datasets=[])

    async def spatial_native(self, operation: str, request):
        self.spatial_calls.append((operation, request))
        if self.spatial_fail_code:
            raise spatial.SpatialFailure(self.spatial_fail_code)
        if operation == "guard_spatial_actor":
            return spatial.SpatialActorResult(**request.model_dump())
        raise AssertionError(f"Unexpected test spatial operation: {operation}")

    async def backfill_scenes(self, request):
        self.spatial_calls.append(("backfill_scenes", request))
        return spatial.LayoutMutationResult(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=0,
            dry_run=request.dry_run,
            notes=[],
        )


class _Runtime:
    def __init__(self, catalog: _Catalog, *, ready: bool = True):
        self.catalog = catalog
        self._is_ready = ready
        self.closed = False

    async def ready(self):
        if not self._is_ready:
            raise RuntimeError("database password is not ready")
        return True

    async def close(self):
        self.closed = True


@pytest.fixture
def client_factory():
    clients = []

    def make_client(catalog: _Catalog | None = None, *, config=None, runtime=None):
        actual_catalog = catalog or _Catalog()
        actual_runtime = runtime or _Runtime(actual_catalog)
        app = create_app(config=config or _config(), runtime=actual_runtime)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client, actual_catalog, actual_runtime

    yield make_client
    for client in clients:
        client.__exit__(None, None, None)


def test_private_routes_require_constant_time_bearer_and_forward_verified_actor(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory()
    actor_id = uuid4()
    body = {"actor_id": str(actor_id), "limit": 25, "offset": 2}

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}

    unauthorized = client.post("/v1/catalog/list", json=body)
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert catalog.calls == []

    response = client.post(
        "/v1/catalog/list",
        json=body,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema": CATALOG_SCHEMA,
        "ok": True,
        "data": {"items": [], "total": 0, "limit": 25, "offset": 2},
    }
    name, request = catalog.calls[0]
    assert name == "list"
    assert request.actor_id == actor_id


def test_wrong_token_and_missing_service_token_never_call_catalog(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory()
    response = client.post(
        "/v1/catalog/list",
        json={"actor_id": str(uuid4())},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert response.status_code == 401
    assert _TOKEN not in response.text
    assert catalog.calls == []


def test_non_ascii_bearer_token_is_rejected_as_unauthorized(client_factory) -> None:
    client, catalog, _runtime = client_factory()
    response = client.post(
        "/v1/catalog/list",
        json={"actor_id": str(uuid4())},
        headers=[(b"authorization", "Bearer café".encode("latin-1"))],
    )
    assert response.status_code == 401
    assert catalog.calls == []


def test_get_items_create_and_update_use_versioned_response_envelopes(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory()
    actor_id = uuid4()
    dataset_id = uuid4()
    authorization = {"Authorization": f"Bearer {_TOKEN}"}

    get_result = client.post(
        "/v1/catalog/get",
        json={"actor_id": str(actor_id), "dataset_id": str(dataset_id)},
        headers=authorization,
    )
    assert get_result.status_code == 200
    assert get_result.json()["data"]["dataset"]["name"] == "catalog"

    item_result = client.post(
        "/v1/catalog/items",
        json={"actor_id": str(actor_id), "dataset_id": str(dataset_id)},
        headers=authorization,
    )
    assert item_result.status_code == 200
    item = item_result.json()["data"]["items"][0]
    assert item["thumbnail_path"] == "thumbnails/item/preview.webp"
    assert item["metadata"] == {"unmodeled": {"kept": True}}

    operation_id = uuid4()
    create_result = client.post(
        "/v1/catalog/create",
        json={
            "operation_id": str(operation_id),
            "actor_id": str(actor_id),
            "name": "catalog",
        },
        headers=authorization,
    )
    assert create_result.status_code == 200
    assert create_result.json()["data"]["operation_id"] == str(operation_id)

    update_result = client.post(
        "/v1/catalog/update",
        json={
            "operation_id": str(uuid4()),
            "actor_id": str(actor_id),
            "dataset_id": str(dataset_id),
            "patch": {"axis_convention": None},
        },
        headers=authorization,
    )
    assert update_result.status_code == 200
    assert [name for name, _request in catalog.calls] == [
        "get",
        "items",
        "create",
        "update",
    ]
    update_request = catalog.calls[-1][1]
    assert update_request.patch.native_payload() == {"axis_convention": None}


def test_association_routes_use_their_own_versioned_success_and_failure_envelopes(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory()
    actor_id, dataset_id, project_id = uuid4(), uuid4(), uuid4()
    authorization = {"Authorization": f"Bearer {_TOKEN}"}

    dataset_result = client.post(
        "/v1/catalog/dataset-projects",
        json={"actor_id": str(actor_id), "dataset_id": str(dataset_id)},
        headers=authorization,
    )
    assert dataset_result.status_code == 200
    assert dataset_result.json() == {
        "schema": ASSOCIATIONS_SCHEMA,
        "ok": True,
        "data": {"dataset_id": str(dataset_id), "projects": []},
    }

    project_result = client.post(
        "/v1/catalog/project-datasets",
        json={"actor_id": str(actor_id), "project_id": str(project_id)},
        headers=authorization,
    )
    assert project_result.status_code == 200
    assert project_result.json() == {
        "schema": ASSOCIATIONS_SCHEMA,
        "ok": True,
        "data": {"project_id": str(project_id), "datasets": []},
    }

    catalog.association_fail_code = "project_missing"
    failure = client.post(
        "/v1/catalog/project-datasets",
        json={"actor_id": str(actor_id), "project_id": str(project_id)},
        headers=authorization,
    )
    assert failure.status_code == 200
    assert failure.json() == {
        "schema": ASSOCIATIONS_SCHEMA,
        "ok": False,
        "error": {"code": "project_missing"},
    }
    assert [name for name, _request in catalog.calls] == [
        "dataset-projects",
        "project-datasets",
        "project-datasets",
    ]


def test_business_failures_are_typed_but_unexpected_errors_are_sanitized(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory()
    catalog.fail_code = "catalog_manager_required"
    body = {"actor_id": str(uuid4())}
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    response = client.post("/v1/catalog/list", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "schema": CATALOG_SCHEMA,
        "ok": False,
        "error": {"code": "catalog_manager_required"},
    }

    catalog.fail_code = None
    catalog.error_message = "database-password-must-not-leak"
    response = client.post("/v1/catalog/list", json=body, headers=headers)
    assert response.status_code == 503
    assert "database-password-must-not-leak" not in response.text
    assert _TOKEN not in response.text


def test_invalid_and_oversized_requests_have_bounded_generic_responses(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory(config=_config(max_request_bytes=1024))
    headers = {"Authorization": f"Bearer {_TOKEN}"}

    invalid = client.post(
        "/v1/catalog/list",
        json={"actor_id": str(uuid4()), "limit": True},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "Invalid Assets catalog request"}

    oversized = client.post(
        "/v1/catalog/list",
        content=b"x" * 2048,
        headers={**headers, "content-type": "application/json"},
    )
    assert oversized.status_code == 413
    assert "x" * 64 not in oversized.text
    assert catalog.calls == []


def test_readiness_hides_database_failures(client_factory) -> None:
    runtime = _Runtime(_Catalog(), ready=False)
    client, _catalog, _runtime = client_factory(runtime=runtime)
    response = client.get("/ready")
    assert response.status_code == 503
    assert "database password is not ready" not in response.text


def test_oversized_response_is_replaced_with_generic_503(client_factory) -> None:
    client, catalog, _runtime = client_factory(config=_config(max_response_bytes=128))
    catalog.large_metadata = True
    response = client.post(
        "/v1/catalog/items",
        json={"actor_id": str(uuid4()), "dataset_id": str(uuid4())},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Assets catalog response is too large"}
    assert "x" * 64 not in response.text


def test_spatial_operations_are_private_typed_and_use_the_spatial_envelope(
    client_factory,
) -> None:
    client, catalog, _runtime = client_factory()
    actor_id = uuid4()
    dataset_id = uuid4()
    operation_id = uuid4()
    body = {"actor_id": str(actor_id), "access": "manager_write"}

    unauthorized = client.post("/v1/spatial/guard_spatial_actor", json=body)
    assert unauthorized.status_code == 401
    assert catalog.spatial_calls == []

    headers = {"Authorization": f"Bearer {_TOKEN}"}
    response = client.post(
        "/v1/spatial/guard_spatial_actor", json=body, headers=headers
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema": spatial.SPATIAL_SCHEMA,
        "ok": True,
        "data": body,
    }
    assert catalog.spatial_calls[0][0] == "guard_spatial_actor"
    assert catalog.spatial_calls[0][1].actor_id == actor_id

    catalog.spatial_fail_code = "catalog_manager_required"
    failure = client.post("/v1/spatial/guard_spatial_actor", json=body, headers=headers)
    assert failure.status_code == 200
    assert failure.json() == {
        "schema": spatial.SPATIAL_SCHEMA,
        "ok": False,
        "error": {"code": "catalog_manager_required"},
    }
    catalog.spatial_fail_code = None

    backfill = client.post(
        "/v1/spatial/backfill_scenes",
        json={
            "operation_id": str(operation_id),
            "actor_id": str(actor_id),
            "dataset_id": str(dataset_id),
            "mode": "auto",
            "dry_run": True,
        },
        headers=headers,
    )
    assert backfill.status_code == 200
    assert backfill.json()["schema"] == spatial.SPATIAL_SCHEMA
    assert backfill.json()["data"]["dry_run"] is True
    assert catalog.spatial_calls[-1][0] == "backfill_scenes"

    invalid = client.post(
        "/v1/spatial/guard_spatial_actor",
        json={**body, "unexpected": True},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert len(catalog.spatial_calls) == 3
