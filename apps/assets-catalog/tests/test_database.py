import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from aap_assets_catalog.contract import (
    CATALOG_SCHEMA,
    CatalogFailure,
    CreateDatasetRequest,
    DatasetMutationResult,
    GetDatasetRequest,
    ListDatasetsRequest,
    ListItemsRequest,
)
from aap_assets_catalog.associations_contract import (
    ASSOCIATIONS_SCHEMA,
    AssociationFailure,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
)
from aap_assets_catalog import spatial_contract as spatial
from aap_assets_catalog.spatial_contract import SPATIAL_NATIVE_SIGNATURES
from aap_assets_catalog.database import (
    EXPECTED_ASSET_FUNCTIONS,
    ROLE_PREFLIGHT_SQL,
    CatalogDatabase,
    DataContractError,
)


def _dataset() -> dict:
    return {
        "id": str(uuid4()),
        "display_id": "D-1",
        "name": "catalog",
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "has_scenes": False,
        "file_count": 0,
        "total_size": 0,
        "created_by": str(uuid4()),
        "project_count": 0,
        "axis_convention": None,
        "created_at": "2026-10-18T00:00:00Z",
        "updated_at": "2026-10-18T00:00:00Z",
    }


class _Transaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *_args):
        self.events.append("rollback" if exc_type else "commit")
        return False


class _Connection:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.transaction_events = []

    def transaction(self):
        return _Transaction(self.transaction_events)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.result


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self, *, timeout=None):
        yield self.connection

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_list_call_uses_frozen_argument_order_and_decodes_json_text() -> None:
    actor_id = uuid4()
    raw = {
        "schema": CATALOG_SCHEMA,
        "ok": True,
        "data": {"items": [_dataset()], "total": 1, "limit": 25, "offset": 5},
    }
    connection = _Connection(json.dumps(raw))
    database = CatalogDatabase(_Pool(connection))

    result = await database.list_datasets(
        ListDatasetsRequest(
            actor_id=actor_id,
            search="road%",
            data_type="lidar",
            has_scenes=False,
            limit=25,
            offset=5,
        )
    )

    assert result.total == 1
    sql, args = connection.calls[0]
    assert "aap_assets_v1.list_catalog_datasets" in sql
    assert args == (actor_id, "road%", "lidar", False, 25, 5)
    assert connection.transaction_events == ["commit"]


@pytest.mark.asyncio
async def test_create_passes_canonical_default_payload_and_maps_typed_error() -> None:
    actor_id = uuid4()
    operation_id = uuid4()
    dataset = _dataset()
    dataset["created_by"] = str(actor_id)
    connection = _Connection(
        {
            "schema": CATALOG_SCHEMA,
            "ok": True,
            "data": {
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "dataset": dataset,
            },
        }
    )
    database = CatalogDatabase(_Pool(connection))
    result = await database.create_dataset(
        CreateDatasetRequest(
            operation_id=operation_id, actor_id=actor_id, name="catalog"
        )
    )
    assert isinstance(result, DatasetMutationResult)
    sql, args = connection.calls[0]
    assert "aap_assets_v1.create_catalog_dataset" in sql
    assert args[:2] == (operation_id, actor_id)
    assert args[2] == {
        "name": "catalog",
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }

    connection.result = {
        "schema": CATALOG_SCHEMA,
        "ok": False,
        "error": {"code": "catalog_manager_required"},
    }
    with pytest.raises(CatalogFailure, match="catalog_manager_required"):
        await database.create_dataset(
            CreateDatasetRequest(
                operation_id=operation_id, actor_id=actor_id, name="catalog"
            )
        )
    assert connection.transaction_events == ["commit", "rollback"]


@pytest.mark.asyncio
async def test_role_preflight_fails_closed_and_names_exact_twenty_six_functions() -> (
    None
):
    catalog_functions = {
        "list_catalog_datasets(uuid,text,text,boolean,integer,bigint)",
        "get_catalog_dataset(uuid,uuid)",
        "list_catalog_items(uuid,uuid,integer,bigint)",
        "create_catalog_dataset(uuid,uuid,jsonb)",
        "update_catalog_dataset(uuid,uuid,uuid,jsonb)",
        "list_dataset_projects(uuid,uuid)",
        "list_project_datasets(uuid,uuid)",
    }
    spatial_functions = {
        f"{name}({signature})" for name, signature in SPATIAL_NATIVE_SIGNATURES.items()
    }
    assert EXPECTED_ASSET_FUNCTIONS == catalog_functions | spatial_functions
    assert len(EXPECTED_ASSET_FUNCTIONS) == 27
    for signature in EXPECTED_ASSET_FUNCTIONS:
        assert signature in ROLE_PREFLIGHT_SQL
    normalized_preflight = " ".join(ROLE_PREFLIGHT_SQL.split())
    assert "membership.member = login_role.oid" in normalized_preflight
    assert "membership.roleid = login_role.oid" in normalized_preflight
    assert "timestamp with time zone" in normalized_preflight
    assert "oidvectortypes" not in normalized_preflight
    assert (
        "accessible_row.function_oid = expected_row.function_oid"
        in normalized_preflight
    )

    class _PreflightConnection(_Connection):
        async def fetchval(self, sql, *args):
            assert sql == ROLE_PREFLIGHT_SQL
            return False

    database = CatalogDatabase(_Pool(_PreflightConnection(None)))
    with pytest.raises(DataContractError, match="not limited"):
        await database.verify_role_scope()


@pytest.mark.asyncio
async def test_native_contract_mismatch_is_not_exposed_as_business_failure() -> None:
    connection = _Connection(
        {
            "schema": "aap-assets-catalog/v2",
            "ok": True,
            "data": {},
        }
    )
    database = CatalogDatabase(_Pool(connection))
    with pytest.raises(DataContractError, match="outside the frozen contract"):
        await database.create_dataset(
            CreateDatasetRequest(operation_id=uuid4(), actor_id=uuid4(), name="catalog")
        )
    assert connection.transaction_events == ["rollback"]


@pytest.mark.asyncio
async def test_oversized_native_item_reply_rolls_back_before_publication() -> None:
    dataset_id = uuid4()
    raw = {
        "schema": CATALOG_SCHEMA,
        "ok": True,
        "data": {
            "dataset_id": str(dataset_id),
            "items": [
                {
                    "id": str(uuid4()),
                    "dataset_id": str(dataset_id),
                    "file_name": "frame.jpg",
                    "file_path": "legacy/frame.jpg",
                    "file_type": "image",
                    "file_size": 42,
                    "content_hash": None,
                    "width": 10,
                    "height": 20,
                    "metadata": {"large": "x" * 256},
                    "file_url": None,
                    "thumbnail_url": None,
                    "blurhash": None,
                    "created_at": "2026-10-18T00:00:00Z",
                    "thumbnail_path": None,
                }
            ],
            "total": 1,
            "limit": 50,
            "offset": 0,
        },
    }
    connection = _Connection(raw)
    database = CatalogDatabase(_Pool(connection), max_reply_bytes=128)

    with pytest.raises(DataContractError, match="response budget"):
        await database.list_items(
            ListItemsRequest(actor_id=uuid4(), dataset_id=dataset_id)
        )
    assert connection.transaction_events == ["rollback"]


@pytest.mark.asyncio
async def test_utf8_reply_budget_accepts_exact_unicode_boundary_and_rejects_one_below() -> (
    None
):
    actor_id = uuid4()
    dataset = _dataset()
    dataset["name"] = "café"
    raw = {
        "schema": CATALOG_SCHEMA,
        "ok": True,
        "data": {"dataset": dataset},
    }
    reply_size = len(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    request = GetDatasetRequest(actor_id=actor_id, dataset_id=UUID(dataset["id"]))

    exact_connection = _Connection(raw)
    exact_database = CatalogDatabase(
        _Pool(exact_connection), max_reply_bytes=reply_size
    )
    result = await exact_database.get_dataset(request)
    assert result.dataset.name == "café"
    assert exact_connection.transaction_events == ["commit"]

    short_connection = _Connection(raw)
    short_database = CatalogDatabase(
        _Pool(short_connection), max_reply_bytes=reply_size - 1
    )
    with pytest.raises(DataContractError, match="response budget"):
        await short_database.get_dataset(request)
    assert short_connection.transaction_events == ["rollback"]


@pytest.mark.asyncio
async def test_association_read_calls_use_exact_argument_order_and_envelope() -> None:
    actor_id = uuid4()
    dataset_id = uuid4()
    project_id = uuid4()
    dataset_projects = _Connection(
        {
            "schema": ASSOCIATIONS_SCHEMA,
            "ok": True,
            "data": {"dataset_id": str(dataset_id), "projects": []},
        }
    )
    database = CatalogDatabase(_Pool(dataset_projects))

    dataset_result = await database.list_dataset_projects(
        ListDatasetProjectsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert dataset_result.dataset_id == dataset_id
    assert "aap_assets_v1.list_dataset_projects" in dataset_projects.calls[0][0]
    assert dataset_projects.calls[0][1] == (actor_id, dataset_id)

    project_datasets = _Connection(
        {
            "schema": ASSOCIATIONS_SCHEMA,
            "ok": True,
            "data": {"project_id": str(project_id), "datasets": []},
        }
    )
    database = CatalogDatabase(_Pool(project_datasets))
    project_result = await database.list_project_datasets(
        ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
    )

    assert project_result.project_id == project_id
    assert "aap_assets_v1.list_project_datasets" in project_datasets.calls[0][0]
    assert project_datasets.calls[0][1] == (actor_id, project_id)


@pytest.mark.asyncio
async def test_association_failure_and_parent_scope_mismatch_fail_closed() -> None:
    actor_id = uuid4()
    project_id = uuid4()
    failed = _Connection(
        {
            "schema": ASSOCIATIONS_SCHEMA,
            "ok": False,
            "error": {"code": "project_missing"},
        }
    )
    database = CatalogDatabase(_Pool(failed))
    with pytest.raises(AssociationFailure, match="project_missing"):
        await database.list_project_datasets(
            ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
        )
    assert failed.transaction_events == ["rollback"]

    escaped = _Connection(
        {
            "schema": ASSOCIATIONS_SCHEMA,
            "ok": True,
            "data": {"project_id": str(uuid4()), "datasets": []},
        }
    )
    database = CatalogDatabase(_Pool(escaped))
    with pytest.raises(DataContractError, match="requested project scope"):
        await database.list_project_datasets(
            ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
        )


@pytest.mark.asyncio
async def test_spatial_calibration_binding_preserves_the_frozen_native_argument_order():
    operation_id, actor_id, task_id, item_id = uuid4(), uuid4(), uuid4(), uuid4()
    calibration = spatial.WireCalibration(
        extrinsic=[1.0] * 16,
        intrinsic=[1.0] * 9,
    )
    request = spatial.ApplyCalibrationRequest(
        operation_id=operation_id,
        actor_id=actor_id,
        task_id=task_id,
        camera_role="camera_front",
        expected_revision=3,
        expected_digest="a" * 64,
        calibration=calibration,
        canonical_document='{"new":true}',
        current_canonical_document='{"current":true}',
        source_snapshot={"item": str(item_id)},
        audit_context={"request_id": "test"},
    )
    state = spatial.CalibrationState(
        dataset_item_id=item_id,
        revision=4,
        calibration=calibration,
        digest="b" * 64,
        created_at=None,
        source_snapshot=request.source_snapshot,
    )
    connection = _Connection(
        {
            "schema": spatial.SPATIAL_SCHEMA,
            "ok": True,
            "data": {
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "task_id": str(task_id),
                "camera_role": "camera_front",
                "state": state.model_dump(mode="json"),
            },
        }
    )
    database = CatalogDatabase(_Pool(connection))

    result = await database.update_spatial_calibration(request)

    assert result.state.dataset_item_id == item_id
    sql, args = connection.calls[0]
    assert "aap_assets_v1.update_spatial_calibration" in sql
    assert args == (
        operation_id,
        actor_id,
        task_id,
        "camera_front",
        3,
        "a" * 64,
        calibration.model_dump(mode="json"),
        '{"new":true}',
        '{"current":true}',
        {"item": str(item_id)},
        {"request_id": "test"},
    )
    assert connection.transaction_events == ["commit"]


@pytest.mark.asyncio
async def test_spatial_scope_violation_rolls_back_before_exposing_a_reply():
    actor_id = uuid4()
    request = spatial.GuardSpatialActorRequest(actor_id=actor_id, access="manager_read")
    connection = _Connection(
        {
            "schema": spatial.SPATIAL_SCHEMA,
            "ok": True,
            "data": {"actor_id": str(uuid4()), "access": "manager_read"},
        }
    )
    database = CatalogDatabase(_Pool(connection))

    with pytest.raises(DataContractError, match="outside the frozen contract"):
        await database.guard_spatial_actor(request)

    assert connection.transaction_events == ["rollback"]


@pytest.mark.asyncio
async def test_dataset_axis_binding_passes_null_axis_in_frozen_argument_order():
    operation_id, dataset_id = uuid4(), uuid4()
    request = spatial.SetDatasetAxisRequest(
        operation_id=operation_id,
        dataset_id=dataset_id,
        axis_convention=None,
    )
    connection = _Connection(
        {
            "schema": spatial.SPATIAL_SCHEMA,
            "ok": True,
            "data": {
                "operation_id": str(operation_id),
                "dataset_id": str(dataset_id),
                "axis_convention": None,
            },
        }
    )
    database = CatalogDatabase(_Pool(connection))

    result = await database.set_spatial_dataset_axis(request)

    assert result.axis_convention is None
    sql, args = connection.calls[0]
    assert "aap_assets_v1.set_spatial_dataset_axis" in sql
    assert args == (operation_id, dataset_id, None)
    assert connection.transaction_events == ["commit"]
