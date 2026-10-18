import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from aap_assets_catalog.contract import (
    CATALOG_SCHEMA,
    CatalogFailure,
    CreateDatasetRequest,
    DatasetMutationResult,
    ListDatasetsRequest,
    ListItemsRequest,
)
from aap_assets_catalog.database import (
    EXPECTED_CATALOG_FUNCTIONS,
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
async def test_role_preflight_fails_closed_and_names_exact_five_functions() -> None:
    assert EXPECTED_CATALOG_FUNCTIONS == {
        "list_catalog_datasets(uuid,text,text,boolean,integer,bigint)",
        "get_catalog_dataset(uuid,uuid)",
        "list_catalog_items(uuid,uuid,integer,bigint)",
        "create_catalog_dataset(uuid,uuid,jsonb)",
        "update_catalog_dataset(uuid,uuid,uuid,jsonb)",
    }
    for signature in EXPECTED_CATALOG_FUNCTIONS:
        assert signature in ROLE_PREFLIGHT_SQL

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
