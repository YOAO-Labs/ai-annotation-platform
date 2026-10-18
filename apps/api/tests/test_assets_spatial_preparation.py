"""Source preparation rejects partial prefixes and preserves human calibration."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from aap_assets_catalog.spatial_contract import (
    PreparationMutationResult,
    SpatialLayoutPlan,
)
from aap_assets_catalog.spatial_values import SensorCalibration, calibration_digest
from app.db.models.dataset import Dataset, DatasetItem, SensorCalibrationRevision
from tests.test_assets_catalog_native import _call
from tests.test_assets_spatial_native import (
    _APPLY_PREPARATION,
    _accept_pointcloud_creation,
    _json_value,
    _read_preparation,
    _seed_pointcloud_preparation,
    _spatial_error,
    _spatial_result,
)

pytestmark = pytest.mark.asyncio


async def test_preparation_unknown_size_custom_camera_and_human_revision_fence(
    db_session,
):
    scope = await _seed_pointcloud_preparation(db_session, actor_role="super_admin")
    calibration = SensorCalibration(
        extrinsic=[1 if i % 5 == 0 else 0 for i in range(16)],
        intrinsic=[1 if i % 4 == 0 else 0 for i in range(9)],
    )
    original_metadata = {
        "calibration": calibration.model_dump(mode="json"),
        "opaque": {"keep": True},
    }
    camera = DatasetItem(
        id=uuid4(),
        dataset_id=scope["dataset_id"],
        file_name="000001.jpg",
        file_path="legacy/camera/front/000001.jpg",
        file_type="legacy_camera",
        metadata_=original_metadata,
    )
    source = DatasetItem(
        id=uuid4(),
        dataset_id=scope["dataset_id"],
        file_name="front.json",
        file_path="legacy/calib/camera/front.json",
        file_type="other",
        file_size=None,
    )
    db_session.add_all((camera, source))
    dataset = await db_session.get(Dataset, scope["dataset_id"])
    dataset.file_count = 3
    await db_session.flush()
    operation_id = await _accept_pointcloud_creation(db_session, scope, job_id=None)
    empty_plan = SpatialLayoutPlan(
        scenes=[],
        assignments=[],
        created_scenes=0,
        assigned_items=0,
        skipped_items=0,
        dry_run=False,
        notes=[],
    )
    imported = calibration.model_copy(deep=True)
    imported.intrinsic[0] = 3.0

    async def invoke(updates, page):
        return await _call(
            db_session,
            _APPLY_PREPARATION,
            {
                "creation_operation_id": operation_id,
                "snapshot": json.dumps({"snapshot_digest": page.snapshot_digest}),
                "calibration_updates": json.dumps(updates),
                "frame_plan": empty_plan.model_dump_json(),
            },
        )

    def observation(page):
        captured_source = next(item for item in page.items if item.id == source.id)
        return {
            "dataset_item_id": str(camera.id),
            "source_item_id": str(source.id),
            "source_snapshot": captured_source.source_snapshot,
            "source_etag": "opaque-etag",
            "source_size": 123,
            "calibration": imported.model_dump(mode="json"),
        }

    page = await _read_preparation(db_session, operation_id)
    entry = observation(page)
    failed = _json_value(
        await invoke([entry, {**entry, "dataset_item_id": str(uuid4())}], page)
    )
    assert failed["ok"] is False
    assert (
        await db_session.scalar(
            select(DatasetItem.metadata_).where(DatasetItem.id == camera.id)
        )
        == original_metadata
    )

    human = SensorCalibrationRevision(
        id=uuid4(),
        dataset_item_id=camera.id,
        revision=1,
        calibration=calibration.model_dump(mode="json"),
        digest=calibration_digest(calibration),
        created_by=scope["actor_id"],
    )
    db_session.add(human)
    await db_session.flush()
    page = await _read_preparation(db_session, operation_id)
    _spatial_error(
        await invoke([observation(page)], page), "calibration_revision_conflict"
    )
    assert (
        await db_session.scalar(
            select(DatasetItem.metadata_).where(DatasetItem.id == camera.id)
        )
        == original_metadata
    )
    await db_session.execute(
        delete(SensorCalibrationRevision).where(
            SensorCalibrationRevision.id == human.id
        )
    )
    page = await _read_preparation(db_session, operation_id)
    result = _spatial_result(
        await invoke([observation(page)], page), PreparationMutationResult
    )
    assert result.calibrated_items == 1
    metadata = await db_session.scalar(
        select(DatasetItem.metadata_).where(DatasetItem.id == camera.id)
    )
    assert metadata["calibration"]["intrinsic"][0] == 3.0
    assert metadata["opaque"] == {"keep": True}
