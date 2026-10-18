from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.dataset_catalog_operation import DatasetCatalogOperation
from app.db.models.project import Project
from app.db.models.user import User

pytestmark = pytest.mark.asyncio

_LIST = text(
    """
    SELECT aap_assets_v1.list_catalog_datasets(
        CAST(:actor_id AS uuid), CAST(:search AS text), CAST(:data_type AS text),
        CAST(:has_scenes AS boolean), CAST(:limit AS integer), CAST(:offset AS bigint)
    )
    """
)
_GET = text(
    "SELECT aap_assets_v1.get_catalog_dataset(CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid))"
)
_ITEMS = text(
    "SELECT aap_assets_v1.list_catalog_items(CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:limit AS integer), CAST(:offset AS bigint))"
)
_CREATE = text(
    "SELECT aap_assets_v1.create_catalog_dataset(CAST(:operation_id AS uuid), "
    "CAST(:actor_id AS uuid), CAST(:request AS jsonb))"
)
_UPDATE = text(
    "SELECT aap_assets_v1.update_catalog_dataset(CAST(:operation_id AS uuid), "
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), CAST(:patch AS jsonb))"
)
_ROLE_ACCEPTANCE = os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
_CATALOG_URL = "AAP_MODULE_DATA_ASSET_CATALOG_URL"
_CATALOG_FUNCTIONS = (
    "aap_assets_v1.list_catalog_datasets(uuid,text,text,boolean,integer,bigint)",
    "aap_assets_v1.get_catalog_dataset(uuid,uuid)",
    "aap_assets_v1.list_catalog_items(uuid,uuid,integer,bigint)",
    "aap_assets_v1.create_catalog_dataset(uuid,uuid,jsonb)",
    "aap_assets_v1.update_catalog_dataset(uuid,uuid,uuid,jsonb)",
)


def _ok(reply: dict, expected_data: str | None = None) -> dict:
    assert reply["schema"] == "aap-assets-catalog/v1"
    assert reply["ok"] is True
    assert set(reply) == {"schema", "ok", "data"}
    data = reply["data"]
    if expected_data is not None:
        expected_shapes = {
            "items": {"items", "total", "limit", "offset"},
            "dataset": {"dataset"},
            "dataset_id": {"dataset_id", "items", "total", "limit", "offset"},
            "operation_id": {"operation_id", "actor_id", "dataset"},
        }
        assert set(data) == expected_shapes[expected_data]
    return data


def _error(reply: dict, code: str) -> None:
    assert reply == {
        "schema": "aap-assets-catalog/v1",
        "ok": False,
        "error": {"code": code},
    }


async def _call(connection, statement, values: dict):
    return await connection.scalar(statement, values)


async def _seed_actor(db_session, *, role: str = "super_admin", active: bool = True):
    actor_id = uuid.uuid4()
    suffix = actor_id.hex
    database = await db_session.scalar(text("SELECT current_database()"))
    if not database or not database.endswith("_test"):
        pytest.fail("refusing to seed Assets catalog data outside the test database")
    db_session.add(
        User(
            id=actor_id,
            email=f"assets-catalog-{suffix}@example.test",
            name="Assets catalog test actor",
            password_hash="test-only",
            role=role,
            is_active=active,
        )
    )
    await db_session.flush()
    return actor_id


async def _seed_dataset(
    db_session,
    *,
    actor_id: uuid.UUID,
    name: str = "catalog fixture",
    data_type: str = "image",
    file_count: int = 0,
    metadata: dict | None = None,
    dataset_id: uuid.UUID | None = None,
    created_at=None,
) -> uuid.UUID:
    dataset_id = dataset_id or uuid.uuid4()
    suffix = dataset_id.hex[:12]
    dataset = Dataset(
        id=dataset_id,
        display_id=f"DC{suffix}",
        name=name,
        description="catalog description",
        data_type=data_type,
        is_temporal=False,
        file_count=file_count,
        created_by=actor_id,
        metadata_=metadata or {},
    )
    if created_at is not None:
        dataset.created_at = created_at
    db_session.add(dataset)
    await db_session.flush()
    return dataset_id


async def _create_request(
    db_session, actor_id: uuid.UUID, operation_id: uuid.UUID, **overrides
) -> dict:
    request = {
        "name": f"created-{operation_id.hex[:12]}",
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }
    request.update(overrides)
    return await _call(
        db_session,
        _CREATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "request": json.dumps(request),
        },
    )


async def _update_request(
    db_session,
    actor_id: uuid.UUID,
    operation_id: uuid.UUID,
    dataset_id: uuid.UUID,
    patch: dict,
) -> dict:
    return await _call(
        db_session,
        _UPDATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "patch": json.dumps(patch),
        },
    )


async def test_catalog_schema_matches_the_five_native_entrypoints():
    root = Path(__file__).parents[1] / "alembic" / "sql"
    schema = json.loads((root / "0211_dataset_catalog.schema.json").read_text())
    assert schema["$id"] == "urn:aap:assets-catalog:v1"
    assert set(schema["x-postgresql-functions"]) == set(
        (
            "aap_assets_v1.list_catalog_datasets(uuid, text, text, boolean, integer, bigint)",
            "aap_assets_v1.get_catalog_dataset(uuid, uuid)",
            "aap_assets_v1.list_catalog_items(uuid, uuid, integer, bigint)",
            "aap_assets_v1.create_catalog_dataset(uuid, uuid, jsonb)",
            "aap_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb)",
        )
    )
    summary = schema["$defs"]["datasetSummary"]
    assert summary["additionalProperties"] is False
    assert set(summary["required"]) == {
        "id",
        "display_id",
        "name",
        "description",
        "data_type",
        "is_temporal",
        "has_scenes",
        "file_count",
        "total_size",
        "created_by",
        "project_count",
        "axis_convention",
        "created_at",
        "updated_at",
    }
    item = schema["$defs"]["catalogItem"]
    assert item["additionalProperties"] is False
    assert "thumbnail_path" in item["required"]
    assert schema["$defs"]["createDatasetRequest"]["additionalProperties"] is False
    assert schema["$defs"]["datasetPatch"]["additionalProperties"] is False
    assert DatasetCatalogOperation.__tablename__ == "dataset_catalog_operations"
    assert not DatasetCatalogOperation.__table__.foreign_keys


async def test_catalog_list_uses_filters_page_snapshot_and_asset_owned_aggregates(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    lower_id, higher_id = sorted((uuid.uuid4(), uuid.uuid4()))
    lower = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        dataset_id=lower_id,
        name="Needle lower",
        data_type="image",
        file_count=7,
        created_at=created_at,
    )
    higher = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        dataset_id=higher_id,
        name="Needle higher",
        data_type="point_cloud",
        file_count=9,
        metadata={"axis_convention": "raw", "opaque": {"preserved": True}},
        created_at=created_at,
    )
    project_id = uuid.uuid4()
    suffix = project_id.hex[:12]
    db_session.add(
        Project(
            id=project_id,
            display_id=f"PC{suffix}",
            name="Catalog association project",
            type_label="Image",
            type_key="image",
            data_type="image",
            owner_id=actor_id,
        )
    )
    await db_session.flush()
    db_session.add(ProjectDataset(project_id=project_id, dataset_id=higher))
    db_session.add(
        DatasetItem(
            dataset_id=higher,
            file_name="higher.png",
            file_path=f"catalog/{higher}.png",
            file_type="image",
            file_size=2048,
        )
    )
    db_session.add(
        DatasetItem(
            dataset_id=higher,
            file_name="unknown-size.png",
            file_path=f"catalog/{higher}-unknown.png",
            file_type="image",
            file_size=None,
        )
    )
    db_session.add(
        Scene(
            id=uuid.uuid4(),
            display_id=f"SCN{suffix}",
            dataset_id=higher,
            name="Catalog scene",
            created_by=actor_id,
        )
    )
    await db_session.flush()

    lidar = await _call(
        db_session,
        _LIST,
        {
            "actor_id": actor_id,
            "search": "NEEDLE",
            "data_type": "lidar",
            "has_scenes": True,
            "limit": 1,
            "offset": 0,
        },
    )
    image_empty_alias = await _call(
        db_session,
        _LIST,
        {
            "actor_id": actor_id,
            "search": None,
            "data_type": "",
            "has_scenes": None,
            "limit": 10,
            "offset": 0,
        },
    )
    tie_page = await _call(
        db_session,
        _LIST,
        {
            "actor_id": actor_id,
            "search": "Needle",
            "data_type": None,
            "has_scenes": None,
            "limit": 1,
            "offset": 0,
        },
    )

    data = _ok(lidar, "items")
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(higher)
    assert data["items"][0]["file_count"] == 9
    assert data["items"][0]["total_size"] == 2048
    assert data["items"][0]["project_count"] == 1
    assert data["items"][0]["has_scenes"] is True
    assert data["items"][0]["axis_convention"] == "raw"

    empty_data_type = _ok(image_empty_alias, "items")
    assert empty_data_type["total"] == 2
    assert [item["id"] for item in empty_data_type["items"]] == [
        str(higher),
        str(lower),
    ]

    tie_data = _ok(tie_page, "items")
    assert tie_data["total"] == 2
    assert tie_data["items"][0]["id"] == str(higher)


async def test_catalog_get_and_item_page_preserve_item_metadata_and_missing_semantics(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        metadata={"axis_convention": "ros_rep103", "external": {"x": 4}},
    )
    db_session.add(
        DatasetItem(
            dataset_id=dataset_id,
            file_name="asset.png",
            file_path="catalog/asset.png",
            file_type="image",
            file_size=128,
            content_hash="a" * 32,
            width=20,
            height=10,
            metadata_={"calibration": {"opaque": True}, "external": 2},
            thumbnail_path="thumbnails/asset.webp",
            blurhash="blur",
        )
    )
    await db_session.flush()

    got = await _call(
        db_session, _GET, {"actor_id": actor_id, "dataset_id": dataset_id}
    )
    items = await _call(
        db_session,
        _ITEMS,
        {"actor_id": actor_id, "dataset_id": dataset_id, "limit": 20, "offset": 0},
    )
    missing_get = await _call(
        db_session,
        _GET,
        {"actor_id": actor_id, "dataset_id": uuid.uuid4()},
    )
    missing_items = await _call(
        db_session,
        _ITEMS,
        {"actor_id": actor_id, "dataset_id": uuid.uuid4(), "limit": 20, "offset": 0},
    )

    summary = _ok(got, "dataset")["dataset"]
    assert summary["axis_convention"] == "ros_rep103"
    assert _ok(items, "dataset_id")["dataset_id"] == str(dataset_id)
    item = _ok(items, "dataset_id")["items"][0]
    assert item["file_url"] is None
    assert item["thumbnail_url"] is None
    assert item["thumbnail_path"] == "thumbnails/asset.webp"
    assert item["metadata"] == {"calibration": {"opaque": True}, "external": 2}
    _error(missing_get, "dataset_missing")
    _error(missing_items, "dataset_missing")


async def test_catalog_create_replay_conflict_and_receipts_survive_dataset_delete(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    operation_id = uuid.uuid4()
    request = {
        "name": f"replay-{operation_id.hex[:12]}",
        "description": "original",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }
    first = await _call(
        db_session,
        _CREATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "request": json.dumps(request),
        },
    )
    first_data = _ok(first, "operation_id")
    dataset = first_data["dataset"]
    dataset_id = uuid.UUID(dataset["id"])
    assert dataset["display_id"].startswith("D-")
    assert dataset["name"] == request["name"]
    assert first_data["actor_id"] == str(actor_id)

    replay = await _call(
        db_session,
        _CREATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "request": json.dumps(request),
        },
    )
    assert replay == first

    conflict_request = dict(request, description="different")
    conflict = await _call(
        db_session,
        _CREATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "request": json.dumps(conflict_request),
        },
    )
    _error(conflict, "operation_conflict")

    update_id = uuid.uuid4()
    update = await _call(
        db_session,
        _UPDATE,
        {
            "operation_id": update_id,
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "patch": json.dumps({"name": "renamed after create"}),
        },
    )
    assert _ok(update, "operation_id")["dataset"]["name"] == "renamed after create"

    await db_session.execute(
        text("DELETE FROM public.datasets WHERE id=:id"), {"id": dataset_id}
    )
    replay_after_delete = await _call(
        db_session,
        _CREATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "request": json.dumps(request),
        },
    )
    missing_now = await _call(
        db_session, _GET, {"actor_id": actor_id, "dataset_id": dataset_id}
    )
    receipt_count = await db_session.scalar(
        text(
            "SELECT count(*) FROM public.dataset_catalog_operations "
            "WHERE operation_id IN (:create_id, :update_id)"
        ),
        {"create_id": operation_id, "update_id": update_id},
    )
    assert replay_after_delete == first
    _error(missing_now, "dataset_missing")
    assert receipt_count == 2


async def test_catalog_create_outer_rollback_removes_dataset_and_receipt(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    operation_id = uuid.uuid4()
    request = {
        "name": f"rollback-{operation_id.hex[:12]}",
        "description": "rollback",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }
    savepoint = await db_session.begin_nested()
    reply = await _call(
        db_session,
        _CREATE,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "request": json.dumps(request),
        },
    )
    dataset_id = uuid.UUID(_ok(reply, "operation_id")["dataset"]["id"])
    await savepoint.rollback()

    dataset_exists = await db_session.scalar(
        text("SELECT EXISTS(SELECT 1 FROM public.datasets WHERE id=:id)"),
        {"id": dataset_id},
    )
    receipt_exists = await db_session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM public.dataset_catalog_operations "
            "WHERE operation_id=:id)"
        ),
        {"id": operation_id},
    )
    assert dataset_exists is False
    assert receipt_exists is False


async def test_catalog_create_deferred_commit_failure_rolls_back_receipt_and_retry(
    test_engine,
):
    actor_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    name = f"commit-fault-{operation_id.hex[:12]}"
    request = {
        "name": name,
        "description": "deferred commit failure",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    failed_dataset_id: uuid.UUID | None = None
    retried_dataset_id: uuid.UUID | None = None
    try:
        async with maker.begin() as owner:
            database = await owner.scalar(text("SELECT current_database()"))
            if not database or not database.endswith("_test"):
                pytest.fail("refusing to seed commit-failure test data outside test DB")
            owner.add(
                User(
                    id=actor_id,
                    email=f"assets-catalog-commit-{actor_id.hex}@example.test",
                    name="Assets catalog commit actor",
                    password_hash="test-only",
                    role="super_admin",
                    is_active=True,
                )
            )
        connection = await test_engine.connect()
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(
                    "CREATE TEMP TABLE aap_catalog_commit_parent (id uuid PRIMARY KEY)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TEMP TABLE aap_catalog_commit_fault ("
                    "id uuid REFERENCES pg_temp.aap_catalog_commit_parent(id) "
                    "DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
                )
            )
            await connection.execute(
                text("INSERT INTO pg_temp.aap_catalog_commit_fault(id) VALUES (:id)"),
                {"id": uuid.uuid4()},
            )
            failed_reply = await _call(
                connection,
                _CREATE,
                {
                    "operation_id": operation_id,
                    "actor_id": actor_id,
                    "request": json.dumps(request),
                },
            )
            failed_dataset_id = uuid.UUID(
                _ok(failed_reply, "operation_id")["dataset"]["id"]
            )
            assert await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM public.datasets WHERE id=:id)"),
                {"id": failed_dataset_id},
            )
            with pytest.raises(DBAPIError):
                await transaction.commit()
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await connection.close()

        async with test_engine.connect() as observer:
            assert not await observer.scalar(
                text("SELECT EXISTS(SELECT 1 FROM public.datasets WHERE id=:id)"),
                {"id": failed_dataset_id},
            )
            assert not await observer.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM public.dataset_catalog_operations "
                    "WHERE operation_id=:id)"
                ),
                {"id": operation_id},
            )

        async with test_engine.begin() as retry:
            retried = await _call(
                retry,
                _CREATE,
                {
                    "operation_id": operation_id,
                    "actor_id": actor_id,
                    "request": json.dumps(request),
                },
            )
        result = _ok(retried, "operation_id")
        retried_dataset_id = uuid.UUID(result["dataset"]["id"])
        assert retried_dataset_id != failed_dataset_id
        assert result["dataset"]["name"] == name
    finally:
        async with test_engine.begin() as cleanup:
            await cleanup.execute(
                text(
                    "DELETE FROM public.dataset_catalog_operations "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            for dataset_id in (failed_dataset_id, retried_dataset_id):
                if dataset_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM public.datasets WHERE id=:id"),
                        {"id": dataset_id},
                    )
            await cleanup.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": actor_id}
            )


async def test_catalog_update_preserves_patch_presence_metadata_and_noop_timestamp(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        metadata={"axis_convention": "raw", "unknown_extension": {"keep": True}},
    )
    original_updated_at = await db_session.scalar(
        text("SELECT updated_at FROM public.datasets WHERE id=:id"),
        {"id": dataset_id},
    )

    renamed_id = uuid.uuid4()
    rename = await _update_request(
        db_session,
        actor_id,
        renamed_id,
        dataset_id,
        {"name": "renamed catalog dataset", "axis_convention": None},
    )
    renamed = _ok(rename, "operation_id")["dataset"]
    assert renamed["name"] == "renamed catalog dataset"
    assert renamed["axis_convention"] is None
    later_id = uuid.uuid4()
    later = await _update_request(
        db_session,
        actor_id,
        later_id,
        dataset_id,
        {"description": "later committed state"},
    )
    assert (
        _ok(later, "operation_id")["dataset"]["description"] == "later committed state"
    )
    rename_replay = await _update_request(
        db_session,
        actor_id,
        renamed_id,
        dataset_id,
        {"name": "renamed catalog dataset", "axis_convention": None},
    )
    assert rename_replay == rename
    metadata = await db_session.scalar(
        text("SELECT metadata FROM public.datasets WHERE id=:id"),
        {"id": dataset_id},
    )
    changed_updated_at = await db_session.scalar(
        text("SELECT updated_at FROM public.datasets WHERE id=:id"),
        {"id": dataset_id},
    )
    assert metadata == {"unknown_extension": {"keep": True}}
    assert changed_updated_at >= original_updated_at

    no_op_id = uuid.uuid4()
    no_op = await _update_request(
        db_session,
        actor_id,
        no_op_id,
        dataset_id,
        {"name": None, "description": None},
    )
    assert _ok(no_op, "operation_id")["dataset"]["name"] == "renamed catalog dataset"
    no_op_updated_at = await db_session.scalar(
        text("SELECT updated_at FROM public.datasets WHERE id=:id"),
        {"id": dataset_id},
    )
    assert no_op_updated_at == changed_updated_at

    reserved_id = uuid.uuid4()
    reserved = await _update_request(
        db_session,
        actor_id,
        reserved_id,
        dataset_id,
        {"name": "/__aap_trusted_nuscenes__-private/data"},
    )
    _error(reserved, "reserved_namespace")

    invalid_id = uuid.uuid4()
    invalid = await _update_request(
        db_session,
        actor_id,
        invalid_id,
        dataset_id,
        {"unexpected": "field"},
    )
    _error(invalid, "invalid_request")


async def test_catalog_writes_require_active_global_manager_even_on_replay(db_session):
    employee_id = await _seed_actor(db_session, role="employee")
    operation_id = uuid.uuid4()
    denied = await _create_request(db_session, employee_id, operation_id)
    _error(denied, "catalog_manager_required")
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.dataset_catalog_operations "
                "WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        == 0
    )

    manager_id = await _seed_actor(db_session)
    manager_operation = uuid.uuid4()
    created = await _create_request(db_session, manager_id, manager_operation)
    _ok(created, "operation_id")
    actor = await db_session.get(User, manager_id)
    assert actor is not None
    actor.is_active = False
    await db_session.flush()
    replay = await _create_request(db_session, manager_id, manager_operation)
    _error(replay, "account_not_active")


async def test_catalog_rejects_malformed_identity_facts_and_allows_unknown_read_role(
    db_session,
):
    database = await db_session.scalar(text("SELECT current_database()"))
    if not database or not database.endswith("_test"):
        pytest.fail("refusing to replace the identity function outside the test DB")

    actor_id = uuid.uuid4()

    async def replace_identity_reply(reply: dict) -> None:
        encoded = json.dumps(reply, separators=(",", ":"))
        await db_session.execute(
            text(
                "CREATE OR REPLACE FUNCTION aap_identity_v1.lock_active_actor("
                "p_actor_id uuid) RETURNS jsonb LANGUAGE sql VOLATILE "
                "PARALLEL UNSAFE SECURITY DEFINER "
                "SET search_path = pg_catalog, pg_temp "
                f"AS $identity_test$ SELECT '{encoded}'::jsonb $identity_test$"
            )
        )

    malformed_replies = (
        {
            "schema": "aap-active-actor/v1",
            "ok": True,
            "data": "not-an-object",
        },
        {
            "schema": "aap-active-actor/v1",
            "ok": True,
            "data": {
                "actor_id": str(uuid.uuid4()),
                "platform_role": "super_admin",
            },
        },
        {
            "schema": "aap-active-actor/v1",
            "ok": True,
            "data": {"actor_id": str(actor_id), "platform_role": 17},
        },
        {
            "schema": "aap-active-actor/v1",
            "ok": True,
            "data": {
                "actor_id": str(actor_id),
                "platform_role": "super_admin",
                "unexpected": True,
            },
        },
    )
    for identity_reply in malformed_replies:
        savepoint = await db_session.begin_nested()
        try:
            await replace_identity_reply(identity_reply)
            with pytest.raises(DBAPIError) as error:
                await _call(
                    db_session,
                    _GET,
                    {"actor_id": actor_id, "dataset_id": uuid.uuid4()},
                )
            sqlstate = getattr(error.value.orig, "sqlstate", None) or getattr(
                error.value.orig, "pgcode", None
            )
            assert sqlstate == "22023"
        finally:
            await savepoint.rollback()

    savepoint = await db_session.begin_nested()
    try:
        await replace_identity_reply(
            {
                "schema": "aap-active-actor/v1",
                "ok": True,
                "data": {"actor_id": str(actor_id), "platform_role": "future_role"},
            }
        )
        read_reply = await _call(
            db_session,
            _GET,
            {"actor_id": actor_id, "dataset_id": uuid.uuid4()},
        )
        _error(read_reply, "dataset_missing")
        write_reply = await _create_request(db_session, actor_id, uuid.uuid4())
        _error(write_reply, "invalid_account_role")
    finally:
        await savepoint.rollback()


async def test_catalog_update_nowait_contention_returns_without_receipt(test_engine):
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    suffix = dataset_id.hex[:12]
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as owner:
        owner.add(
            User(
                id=actor_id,
                email=f"assets-catalog-lock-{actor_id.hex}@example.test",
                name="Assets catalog lock actor",
                password_hash="test-only",
                role="super_admin",
                is_active=True,
            )
        )
        await owner.flush()
        owner.add(
            Dataset(
                id=dataset_id,
                display_id=f"DL{suffix}",
                name="locked",
                description="catalog description",
                data_type="image",
                file_count=0,
                created_by=actor_id,
            )
        )
    operation_id = uuid.uuid4()
    blocker = await test_engine.connect()
    blocker_tx = await blocker.begin()
    try:
        await blocker.execute(
            text("SELECT id FROM public.datasets WHERE id=:id FOR UPDATE"),
            {"id": dataset_id},
        )
        async with test_engine.connect() as caller:
            busy = await _call(
                caller,
                _UPDATE,
                {
                    "operation_id": operation_id,
                    "actor_id": actor_id,
                    "dataset_id": dataset_id,
                    "patch": json.dumps({"name": "must not write"}),
                },
            )
    finally:
        await blocker_tx.rollback()
        await blocker.close()

    try:
        _error(busy, "source_busy")
        async with test_engine.connect() as owner:
            name = await owner.scalar(
                text("SELECT name FROM public.datasets WHERE id=:id"),
                {"id": dataset_id},
            )
            receipt_exists = await owner.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM public.dataset_catalog_operations "
                    "WHERE operation_id=:id)"
                ),
                {"id": operation_id},
            )
        assert name == "locked"
        assert receipt_exists is False
    finally:
        async with test_engine.begin() as cleanup:
            await cleanup.execute(
                text(
                    "DELETE FROM public.dataset_catalog_operations WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            await cleanup.execute(
                text("DELETE FROM public.datasets WHERE id=:id"),
                {"id": dataset_id},
            )
            await cleanup.execute(
                text("DELETE FROM public.users WHERE id=:id"),
                {"id": actor_id},
            )


@pytest.mark.skipif(
    not _ROLE_ACCEPTANCE,
    reason="requires the disposable module-data boundary verifier",
)
async def test_catalog_login_is_limited_to_the_five_catalog_capabilities():
    raw_url = os.environ.get(_CATALOG_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_CATALOG_URL}")
    url = make_url(raw_url)
    if url.username != "asset_catalog_client" or not url.database.endswith("_test"):
        pytest.fail("catalog LOGIN URL does not target the owned test database")
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as catalog:
            assert (
                await catalog.scalar(text("SELECT current_user"))
                == "asset_catalog_client"
            )
            assert (
                await catalog.scalar(text("SELECT session_user"))
                == "asset_catalog_client"
            )
            assert await catalog.scalar(
                text(
                    "SELECT has_schema_privilege(current_user, 'aap_assets_v1', 'USAGE')"
                )
            )
            assert not await catalog.scalar(
                text(
                    "SELECT has_schema_privilege(current_user, 'aap_assets_v1', 'CREATE')"
                )
            )
            for signature in _CATALOG_FUNCTIONS:
                assert await catalog.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                    ),
                    {"signature": signature},
                )
            assert not await catalog.scalar(
                text(
                    "SELECT has_function_privilege(current_user, "
                    "'aap_assets_v1._catalog_actor(uuid,boolean)', 'EXECUTE')"
                )
            )
            allowed = await catalog.execute(
                text(
                    "SELECT proc.oid::regprocedure::text "
                    "FROM pg_catalog.pg_proc AS proc "
                    "WHERE proc.pronamespace = 'aap_assets_v1'::regnamespace "
                    "AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')"
                )
            )
            assert {row[0].replace(", ", ",") for row in allowed} == {
                signature.replace(", ", ",") for signature in _CATALOG_FUNCTIONS
            }
            assert not await catalog.scalar(
                text(
                    "SELECT has_table_privilege(current_user, 'public.datasets', 'SELECT')"
                )
            )
            assert not await catalog.scalar(
                text(
                    "SELECT has_table_privilege(current_user, "
                    "'public.dataset_catalog_operations', 'SELECT')"
                )
            )
            assert not await catalog.scalar(
                text(
                    "SELECT has_sequence_privilege(current_user, "
                    "'public.display_seq_datasets', 'USAGE')"
                )
            )
            with pytest.raises(DBAPIError):
                await catalog.execute(text("SELECT count(*) FROM public.datasets"))
            await catalog.rollback()
    finally:
        await engine.dispose()
