"""Native spatial receipt lookup and bounded scene-page regressions."""

from __future__ import annotations

import json
import uuid
from uuid import UUID

import pytest
from sqlalchemy import text

from aap_assets_catalog.spatial_contract import (
    ItemFactsPage,
    LayoutMutationResult,
    LayoutPage,
    SpatialLayoutPlan,
    SPATIAL_SCHEMA,
    decode_spatial_reply,
)
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.user import User
from tests.test_assets_catalog_native import _call, _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_READ_LAYOUT = text(
    "SELECT aap_assets_v1.read_spatial_layout("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), NULL, "
    "CAST(:after_item_id AS uuid), CAST(:page_size AS integer))"
)
_READ_ITEMS = text(
    "SELECT aap_assets_v1.read_spatial_items("
    "CAST(:dataset_id AS uuid), NULL, CAST(:after_item_id AS uuid), "
    "CAST(:page_size AS integer))"
)
_READ_ITEMS_BATCH = text(
    "SELECT aap_assets_v1.read_spatial_items("
    "NULL, CAST(:item_ids AS uuid[]), CAST(:after_item_id AS uuid), "
    "CAST(:page_size AS integer))"
)
_APPLY_LAYOUT = text(
    "SELECT aap_assets_v1.apply_spatial_layout("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:mode AS text), "
    "CAST(:dry_run AS boolean), CAST(:plan AS jsonb), CAST(:snapshot AS jsonb))"
)
_READ_LAYOUT_RECEIPT = text(
    "SELECT aap_assets_v1.read_spatial_layout_receipt("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:mode AS text), CAST(:dry_run AS boolean))"
)


def _payload(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _result(value: object, model):
    return decode_spatial_reply(_payload(value), model)


def _error(value: object, code: str) -> None:
    assert _payload(value) == {
        "schema": SPATIAL_SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


async def _layout_page(
    db_session,
    *,
    actor_id: UUID,
    dataset_id: UUID,
    after_item_id: UUID | None,
    page_size: int,
) -> LayoutPage:
    return _result(
        await _call(
            db_session,
            _READ_LAYOUT,
            {
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "after_item_id": after_item_id,
                "page_size": page_size,
            },
        ),
        LayoutPage,
    )


async def test_layout_receipt_lookup_miss_is_success_with_null_result(db_session):
    operation_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    reply = _payload(
        await _call(
            db_session,
            _READ_LAYOUT_RECEIPT,
            {
                "operation_id": operation_id,
                "actor_id": None,
                "dataset_id": dataset_id,
                "mode": "auto",
                "dry_run": False,
            },
        )
    )

    assert reply == {
        "schema": SPATIAL_SCHEMA,
        "ok": True,
        "data": {
            "operation_id": str(operation_id),
            "actor_id": None,
            "dataset_id": str(dataset_id),
            "mode": "auto",
            "dry_run": False,
            "result": None,
        },
    }


async def test_layout_receipt_survives_changed_layout_and_still_checks_actor(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, file_count=0)
    operation_id = uuid.uuid4()
    before = await _layout_page(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        after_item_id=None,
        page_size=1,
    )
    plan = SpatialLayoutPlan(
        scenes=[],
        assignments=[],
        created_scenes=0,
        assigned_items=0,
        skipped_items=0,
        dry_run=False,
        notes=[],
    )
    applied = _result(
        await _call(
            db_session,
            _APPLY_LAYOUT,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "mode": "auto",
                "dry_run": False,
                "plan": json.dumps(plan.model_dump(mode="json")),
                "snapshot": json.dumps({"snapshot_digest": before.snapshot_digest}),
            },
        ),
        LayoutMutationResult,
    )

    scene_id = uuid.uuid4()
    db_session.add(
        Scene(
            id=scene_id,
            display_id=f"SCNR-{scene_id.hex[:10]}",
            dataset_id=dataset_id,
            name="layout changed after apply",
            source_format="native-test",
            source_metadata={},
            created_by=actor_id,
        )
    )
    await db_session.flush()
    after_change = await _layout_page(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        after_item_id=None,
        page_size=1,
    )
    assert after_change.scene_count == 1
    assert after_change.snapshot_digest != before.snapshot_digest

    receipt = _payload(
        await _call(
            db_session,
            _READ_LAYOUT_RECEIPT,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "mode": "auto",
                "dry_run": False,
            },
        )
    )
    assert receipt["ok"] is True
    assert receipt["data"]["result"] == applied.model_dump(mode="json")

    conflict = await _call(
        db_session,
        _READ_LAYOUT_RECEIPT,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "mode": "single",
            "dry_run": False,
        },
    )
    _error(conflict, "operation_conflict")

    actor = await db_session.get(User, actor_id)
    assert actor is not None
    actor.role = "employee"
    await db_session.flush()
    revoked = await _call(
        db_session,
        _READ_LAYOUT_RECEIPT,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "mode": "auto",
            "dry_run": False,
        },
    )
    _error(revoked, "catalog_manager_required")


async def test_layout_and_item_pages_return_only_referenced_scene_summaries(db_session):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, file_count=2)
    item_ids = sorted((uuid.uuid4(), uuid.uuid4()))
    scene_ids = [uuid.uuid4(), uuid.uuid4()]
    for index, (item_id, scene_id) in enumerate(zip(item_ids, scene_ids, strict=True)):
        db_session.add(
            Scene(
                id=scene_id,
                display_id=f"SCNR-{scene_id.hex[:10]}",
                dataset_id=dataset_id,
                name=f"scene-{index}",
                source_format="native-test",
                source_metadata={},
                created_by=actor_id,
            )
        )
        db_session.add(
            DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name=f"frame-{index}.pcd",
                file_path=f"spatial/{dataset_id}/frame-{index}.pcd",
                file_type="point_cloud",
                scene_id=scene_id,
                frame_index=0,
                metadata_={},
            )
        )
    await db_session.flush()

    layout_first = await _layout_page(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        after_item_id=None,
        page_size=1,
    )
    layout_second = await _layout_page(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        after_item_id=layout_first.next_item_id,
        page_size=1,
    )
    assert layout_first.complete is False
    assert layout_second.complete is True
    assert layout_first.snapshot_digest == layout_second.snapshot_digest
    assert [scene.id for scene in layout_first.scenes] == [
        layout_first.items[0].scene_id
    ]
    assert [scene.id for scene in layout_second.scenes] == [
        layout_second.items[0].scene_id
    ]

    items_first = _result(
        await _call(
            db_session,
            _READ_ITEMS,
            {
                "dataset_id": dataset_id,
                "after_item_id": None,
                "page_size": 1,
            },
        ),
        ItemFactsPage,
    )
    items_second = _result(
        await _call(
            db_session,
            _READ_ITEMS,
            {
                "dataset_id": dataset_id,
                "after_item_id": items_first.next_item_id,
                "page_size": 1,
            },
        ),
        ItemFactsPage,
    )
    assert items_first.snapshot_digest == items_second.snapshot_digest
    assert items_first.snapshot_digest == layout_first.snapshot_digest
    assert [scene.id for scene in items_first.scenes] == [items_first.items[0].scene_id]
    assert [scene.id for scene in items_second.scenes] == [
        items_second.items[0].scene_id
    ]

    batch_first = _result(
        await _call(
            db_session,
            _READ_ITEMS_BATCH,
            {"item_ids": item_ids, "after_item_id": None, "page_size": 1},
        ),
        ItemFactsPage,
    )
    batch_second = _result(
        await _call(
            db_session,
            _READ_ITEMS_BATCH,
            {
                "item_ids": item_ids,
                "after_item_id": batch_first.next_item_id,
                "page_size": 1,
            },
        ),
        ItemFactsPage,
    )
    assert batch_first.snapshot_digest == batch_second.snapshot_digest
    assert [scene.id for scene in batch_first.scenes] == [batch_first.items[0].scene_id]
    assert [scene.id for scene in batch_second.scenes] == [
        batch_second.items[0].scene_id
    ]
