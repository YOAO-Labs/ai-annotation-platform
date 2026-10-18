"""Native Assets calibration read/update and source provenance regressions."""

from __future__ import annotations

import json
import math
import uuid
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from aap_assets_catalog.spatial_contract import (
    CalibrationMutationResult,
    CalibrationPage,
    CameraTargetResult,
    ProvenanceMutationResult,
    canonical_calibration_document,
    decode_spatial_reply,
    normalize_calibration_record,
)
from aap_assets_catalog.spatial_values import SensorCalibration, calibration_digest
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import DatasetItem, SensorCalibrationRevision
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from tests.factory import create_project
from tests.test_assets_catalog_native import _call, _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_READ_CALIBRATIONS = text(
    "SELECT aap_assets_v1.read_spatial_calibrations("
    "CAST(:dataset_item_ids AS uuid[]), CAST(:include_history AS boolean), "
    "CAST(:after_item_id AS uuid), CAST(:after_revision AS integer), "
    "CAST(:page_size AS integer))"
)
_UPDATE_CALIBRATION = text(
    "SELECT aap_assets_v1.update_spatial_calibration("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:camera_role AS text), "
    "CAST(:expected_revision AS integer), CAST(:expected_digest AS text), "
    "CAST(:calibration AS jsonb), CAST(:canonical_document AS text), "
    "CAST(:current_canonical_document AS text), CAST(:source_snapshot AS jsonb), "
    "CAST(:audit_context AS jsonb))"
)
_RECORD_PROVENANCE = text(
    "SELECT aap_assets_v1.record_spatial_calibration_provenance("
    "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid), "
    "CAST(:entries AS jsonb))"
)
_READ_CAMERA_TARGET = text(
    "SELECT aap_assets_v1.read_spatial_camera_target("
    "CAST(:actor_id AS uuid),CAST(:task_id AS uuid),'camera_front')"
)


def _json_value(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _spatial_result(value: object, result_type):
    return decode_spatial_reply(_json_value(value), result_type)


def _spatial_error(value: object, code: str) -> None:
    assert _json_value(value) == {
        "schema": "aap-assets-spatial/v1",
        "ok": False,
        "error": {"code": code},
    }


def _calibration(*, scale: float = 1.0) -> SensorCalibration:
    return SensorCalibration(
        extrinsic=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        intrinsic=[scale, 0, 0, 0, 1, 0, 0, 0, 1],
        rect=None,
    )


async def test_camera_target_read_uses_current_link_and_visibility(db_session):
    fixture = await _seed_camera_fixture(db_session)
    result = _spatial_result(
        await _call(db_session, _READ_CAMERA_TARGET, fixture), CameraTargetResult
    )
    assert result.dataset_item_id == fixture["dataset_item_id"]
    assert result.actor_id == fixture["actor_id"]
    assert result.task_id == fixture["task_id"]
    await db_session.execute(
        text("UPDATE users SET is_active=false WHERE id=:actor_id"), fixture
    )
    _spatial_error(
        await _call(db_session, _READ_CAMERA_TARGET, fixture), "account_not_active"
    )
    await db_session.execute(
        text("UPDATE users SET is_active=true WHERE id=:actor_id"), fixture
    )
    await db_session.execute(
        text("DELETE FROM task_dataset_item_links WHERE task_id=:task_id"), fixture
    )
    _spatial_error(
        await _call(db_session, _READ_CAMERA_TARGET, fixture), "camera_role_not_linked"
    )


async def _seed_camera_task(
    db_session,
    *,
    actor_id: UUID,
    item_id: UUID,
    role: str = "camera_front",
) -> tuple[Project, Task]:
    project = await create_project(
        db_session,
        owner_id=actor_id,
        type_key="lidar",
        type_label="Point cloud",
    )
    project.data_type = "lidar"
    task = Task(
        project_id=project.id,
        display_id=f"T-SP-{uuid.uuid4().hex[:10]}",
        file_name="frame.pcd",
        file_path="spatial/frame.pcd",
        file_type="point_cloud",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(
        TaskDatasetItemLink(
            task_id=task.id,
            dataset_item_id=item_id,
            role=role,
            sensor_name=role.removeprefix("camera_"),
        )
    )
    await db_session.flush()
    return project, task


async def _seed_camera_fixture(
    db_session,
    *,
    actor_role: str = "project_admin",
    export_metadata: dict | None = None,
) -> dict:
    actor_id = await _seed_actor(db_session, role=actor_role)
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"spatial-calibration-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=1,
    )
    calibration = _calibration()
    metadata = {
        "calibration": calibration.model_dump(mode="json"),
        "unrelated": {"preserve": True},
    }
    if export_metadata is not None:
        metadata["nuscenes_export"] = export_metadata
    item = DatasetItem(
        dataset_id=dataset_id,
        file_name="camera.jpg",
        file_path=f"spatial/{dataset_id}/camera/front.jpg",
        file_type="image",
        width=1280,
        height=720,
        metadata_=metadata,
    )
    db_session.add(item)
    await db_session.flush()
    project, task = await _seed_camera_task(
        db_session, actor_id=actor_id, item_id=item.id
    )
    return {
        "actor_id": actor_id,
        "dataset_id": dataset_id,
        "dataset_item_id": item.id,
        "task_id": task.id,
        "project": project,
        "calibration": calibration,
        "metadata": metadata,
    }


async def _calibration_page(
    db_session, item_ids: list[UUID], *, include_history: bool = False
) -> CalibrationPage:
    reply = await _call(
        db_session,
        _READ_CALIBRATIONS,
        {
            "dataset_item_ids": item_ids,
            "include_history": include_history,
            "after_item_id": None,
            "after_revision": None,
            "page_size": 100,
        },
    )
    return _spatial_result(reply, CalibrationPage)


def _update_values(
    *,
    operation_id: UUID,
    actor_id: UUID,
    task_id: UUID,
    calibration: SensorCalibration,
    expected_revision: int,
    expected_digest: str,
    current_canonical_document: str,
    source_snapshot: dict,
) -> dict:
    return {
        "operation_id": operation_id,
        "actor_id": actor_id,
        "task_id": task_id,
        "camera_role": "camera_front",
        "expected_revision": expected_revision,
        "expected_digest": expected_digest,
        "calibration": json.dumps(calibration.model_dump(mode="json")),
        "current_canonical_document": current_canonical_document,
        "canonical_document": canonical_calibration_document(calibration),
        "source_snapshot": json.dumps(source_snapshot),
        "audit_context": json.dumps(
            {
                "method": "PATCH",
                "path": f"/api/v1/tasks/{task_id}/point-cloud/cameras/camera_front/calibration",
                "request_id": str(operation_id),
            }
        ),
    }


async def _audit_count(db_session, item_id: UUID) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "sensor_calibration.update")
            .where(AuditLog.target_type == "dataset_item")
            .where(AuditLog.target_id == str(item_id))
        )
        or 0
    )


async def _receipt_count(db_session, operation_id: UUID) -> int:
    return int(
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.dataset_spatial_operations "
                "WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        or 0
    )


async def _provenance_entries(db_session, item_ids: list[UUID]) -> list[dict]:
    page = await _calibration_page(db_session, item_ids)
    return [
        {
            "dataset_item_id": str(record.dataset_item_id),
            "calibration": normalize_calibration_record(record).calibration.model_dump(
                mode="json"
            ),
            "digest": normalize_calibration_record(record).digest,
            "canonical_document": canonical_calibration_document(
                normalize_calibration_record(record).calibration
            ),
            "source_snapshot": record.source_snapshot,
        }
        for record in page.records
    ]


async def test_legacy_coercible_calibration_and_linked_custom_file_type_can_update(
    db_session,
):
    fixture = await _seed_camera_fixture(db_session)
    item = await db_session.get(DatasetItem, fixture["dataset_item_id"])
    raw = fixture["calibration"].model_dump(mode="json")
    raw["extrinsic"][1] = False
    raw["intrinsic"][0] = "123.5"
    item.metadata_ = {"calibration": raw, "unrelated": {"preserve": True}}
    item.file_type = "legacy_camera"
    await db_session.flush()
    page = await _calibration_page(db_session, [item.id])
    current = normalize_calibration_record(page.records[0])
    assert current.calibration.intrinsic[0] == 123.5
    assert current.calibration.extrinsic[1] == 0.0
    changed = _calibration(scale=2.0)
    operation_id = uuid.uuid4()
    reply = await _call(
        db_session,
        _UPDATE_CALIBRATION,
        _update_values(
            operation_id=operation_id,
            actor_id=fixture["actor_id"],
            task_id=fixture["task_id"],
            calibration=changed,
            expected_revision=current.revision,
            expected_digest=current.digest,
            current_canonical_document=canonical_calibration_document(
                current.calibration
            ),
            source_snapshot=current.source_snapshot,
        ),
    )
    result = _spatial_result(reply, CalibrationMutationResult)
    assert result.state.revision == 2
    assert result.state.digest == calibration_digest(changed)
    metadata = await db_session.scalar(
        select(DatasetItem.metadata_).where(DatasetItem.id == item.id)
    )
    assert metadata["unrelated"] == {"preserve": True}
    assert await _audit_count(db_session, item.id) == 1


async def test_provenance_late_invalid_entry_does_not_persist_valid_prefix(db_session):
    fixture = await _seed_camera_fixture(db_session, export_metadata={"sample": "kept"})
    item_id = fixture["dataset_item_id"]
    entries = await _provenance_entries(db_session, [item_id])
    entries.append({**entries[0], "dataset_item_id": str(uuid.uuid4())})
    operation_id = uuid.uuid4()
    reply = _json_value(
        await _call(
            db_session,
            _RECORD_PROVENANCE,
            {
                "operation_id": operation_id,
                "dataset_id": fixture["dataset_id"],
                "entries": json.dumps(entries, default=str),
            },
        )
    )
    assert reply["ok"] is False
    metadata = await db_session.scalar(
        select(DatasetItem.metadata_).where(DatasetItem.id == item_id)
    )
    assert metadata["nuscenes_export"] == {"sample": "kept"}
    assert await _receipt_count(db_session, operation_id) == 0


async def test_legacy_calibration_cas_preserves_metadata_and_rolls_back_audit_and_receipt(
    db_session,
) -> None:
    fixture = await _seed_camera_fixture(db_session)
    item_id = fixture["dataset_item_id"]
    current_page = await _calibration_page(db_session, [item_id])
    record = current_page.records[0]
    current = normalize_calibration_record(record)
    assert record.revision == 1
    assert record.digest is None
    assert record.created_at is None
    assert current.revision == 1
    assert current.digest == calibration_digest(fixture["calibration"])
    assert record.source_snapshot["dataset_item_id"] == str(item_id)
    assert record.source_snapshot["dataset_id"] == str(fixture["dataset_id"])

    updated = _calibration(scale=2.0)
    operation_id = uuid.uuid4()
    update_values = _update_values(
        operation_id=operation_id,
        actor_id=fixture["actor_id"],
        task_id=fixture["task_id"],
        calibration=updated,
        expected_revision=current.revision,
        expected_digest=current.digest,
        current_canonical_document=canonical_calibration_document(current.calibration),
        source_snapshot=record.source_snapshot,
    )

    savepoint = await db_session.begin_nested()
    rolled_back_reply = await _call(db_session, _UPDATE_CALIBRATION, update_values)
    rolled_back = _spatial_result(rolled_back_reply, CalibrationMutationResult)
    assert rolled_back.state.revision == 2
    assert await _audit_count(db_session, item_id) == 1
    assert await _receipt_count(db_session, operation_id) == 1
    await savepoint.rollback()

    metadata_after_rollback = await db_session.scalar(
        text("SELECT metadata FROM public.dataset_items WHERE id=:id"),
        {"id": item_id},
    )
    assert metadata_after_rollback == fixture["metadata"]
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.sensor_calibration_revisions "
                "WHERE dataset_item_id=:id"
            ),
            {"id": item_id},
        )
        == 0
    )
    assert await _audit_count(db_session, item_id) == 0
    assert await _receipt_count(db_session, operation_id) == 0

    committed_reply = await _call(db_session, _UPDATE_CALIBRATION, update_values)
    committed = _spatial_result(committed_reply, CalibrationMutationResult)
    assert committed.state.revision == 2
    assert committed.state.digest == calibration_digest(updated)
    replay = await _call(db_session, _UPDATE_CALIBRATION, update_values)
    assert _json_value(replay) == _json_value(committed_reply)
    assert await _audit_count(db_session, item_id) == 1
    assert await _receipt_count(db_session, operation_id) == 1

    metadata = await db_session.scalar(
        text("SELECT metadata FROM public.dataset_items WHERE id=:id"),
        {"id": item_id},
    )
    assert metadata["calibration"] == updated.model_dump(mode="json")
    assert metadata["unrelated"] == {"preserve": True}
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.sensor_calibration_revisions "
                "WHERE dataset_item_id=:id"
            ),
            {"id": item_id},
        )
        == 2
    )

    current_after_update = (await _calibration_page(db_session, [item_id])).records[0]
    updated_state = normalize_calibration_record(current_after_update)
    no_op_id = uuid.uuid4()
    no_op = _spatial_result(
        await _call(
            db_session,
            _UPDATE_CALIBRATION,
            _update_values(
                operation_id=no_op_id,
                actor_id=fixture["actor_id"],
                task_id=fixture["task_id"],
                calibration=updated,
                expected_revision=updated_state.revision,
                expected_digest=updated_state.digest,
                current_canonical_document=canonical_calibration_document(
                    updated_state.calibration
                ),
                source_snapshot=current_after_update.source_snapshot,
            ),
        ),
        CalibrationMutationResult,
    )
    assert no_op.state.revision == 2
    assert await _audit_count(db_session, item_id) == 2
    assert await _receipt_count(db_session, no_op_id) == 1
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.sensor_calibration_revisions "
                "WHERE dataset_item_id=:id"
            ),
            {"id": item_id},
        )
        == 2
    )

    stale_id = uuid.uuid4()
    stale = await _call(
        db_session,
        _UPDATE_CALIBRATION,
        _update_values(
            operation_id=stale_id,
            actor_id=fixture["actor_id"],
            task_id=fixture["task_id"],
            calibration=fixture["calibration"],
            expected_revision=1,
            expected_digest=current.digest,
            current_canonical_document=canonical_calibration_document(
                updated_state.calibration
            ),
            source_snapshot=current_after_update.source_snapshot,
        ),
    )
    _spatial_error(stale, "calibration_revision_conflict")
    assert await _audit_count(db_session, item_id) == 2
    assert await _receipt_count(db_session, stale_id) == 0


async def test_project_owner_cannot_update_camera_shared_with_another_project(
    db_session,
) -> None:
    owner_a = await _seed_actor(db_session, role="project_admin")
    owner_b = await _seed_actor(db_session, role="project_admin")
    super_admin = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=owner_a,
        name=f"shared-camera-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=1,
    )
    calibration = _calibration()
    item = DatasetItem(
        dataset_id=dataset_id,
        file_name="shared.jpg",
        file_path=f"shared/{dataset_id}/camera/front.jpg",
        file_type="image",
        width=1280,
        height=720,
        metadata_={"calibration": calibration.model_dump(mode="json")},
    )
    db_session.add(item)
    await db_session.flush()
    _project_a, task_a = await _seed_camera_task(
        db_session, actor_id=owner_a, item_id=item.id
    )
    _project_b, _task_b = await _seed_camera_task(
        db_session, actor_id=owner_b, item_id=item.id
    )
    legacy = (await _calibration_page(db_session, [item.id])).records[0]
    state = normalize_calibration_record(legacy)
    changed = _calibration(scale=3.0)
    denied_operation = uuid.uuid4()
    denied = await _call(
        db_session,
        _UPDATE_CALIBRATION,
        _update_values(
            operation_id=denied_operation,
            actor_id=owner_a,
            task_id=task_a.id,
            calibration=changed,
            expected_revision=state.revision,
            expected_digest=state.digest,
            current_canonical_document=canonical_calibration_document(
                state.calibration
            ),
            source_snapshot=legacy.source_snapshot,
        ),
    )
    _spatial_error(denied, "camera_shared_across_projects")
    assert await _audit_count(db_session, item.id) == 0
    assert await _receipt_count(db_session, denied_operation) == 0

    authorized_operation = uuid.uuid4()
    authorized = _spatial_result(
        await _call(
            db_session,
            _UPDATE_CALIBRATION,
            _update_values(
                operation_id=authorized_operation,
                actor_id=super_admin,
                task_id=task_a.id,
                calibration=changed,
                expected_revision=state.revision,
                expected_digest=state.digest,
                current_canonical_document=canonical_calibration_document(
                    state.calibration
                ),
                source_snapshot=legacy.source_snapshot,
            ),
        ),
        CalibrationMutationResult,
    )
    assert authorized.state.revision == 2
    assert await _audit_count(db_session, item.id) == 1
    assert await _receipt_count(db_session, authorized_operation) == 1


async def test_historical_digest_survives_jsonb_negative_zero_normalization(
    db_session,
) -> None:
    fixture = await _seed_camera_fixture(db_session)
    item_id = fixture["dataset_item_id"]
    original = _calibration(scale=-0.0)
    original_document = canonical_calibration_document(original)
    original_digest = calibration_digest(original)
    assert "-0.0" in original_document

    # PostgreSQL JSONB normalizes -0.0 to 0.0, while the append-only digest
    # records the exact canonical input document that existed before storage.
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET metadata = jsonb_set("
            "metadata, '{calibration}', CAST(:calibration AS jsonb)) WHERE id=:id"
        ),
        {
            "id": item_id,
            "calibration": json.dumps(original.model_dump(mode="json")),
        },
    )
    stored_calibration = await db_session.scalar(
        text("SELECT metadata->'calibration' FROM public.dataset_items WHERE id=:id"),
        {"id": item_id},
    )
    assert stored_calibration["intrinsic"][0] == 0.0
    assert math.copysign(1.0, stored_calibration["intrinsic"][0]) == 1.0

    db_session.add(
        SensorCalibrationRevision(
            dataset_item_id=item_id,
            revision=1,
            digest=original_digest,
            calibration=stored_calibration,
            created_by=fixture["actor_id"],
        )
    )
    await db_session.flush()

    historical = (await _calibration_page(db_session, [item_id])).records[0]
    assert historical.revision == 1
    assert historical.digest == original_digest
    state = normalize_calibration_record(historical)
    assert state.digest == original_digest
    assert state.calibration.intrinsic[0] == 0.0
    assert math.copysign(1.0, state.calibration.intrinsic[0]) == 1.0

    updated = _calibration(scale=2.0)
    operation_id = uuid.uuid4()
    reply = await _call(
        db_session,
        _UPDATE_CALIBRATION,
        _update_values(
            operation_id=operation_id,
            actor_id=fixture["actor_id"],
            task_id=fixture["task_id"],
            calibration=updated,
            expected_revision=state.revision,
            expected_digest=state.digest,
            current_canonical_document=canonical_calibration_document(
                state.calibration
            ),
            source_snapshot=historical.source_snapshot,
        ),
    )
    mutation = _spatial_result(reply, CalibrationMutationResult)
    assert mutation.state.revision == 2
    assert mutation.state.digest == calibration_digest(updated)
    assert await _audit_count(db_session, item_id) == 1
    assert await _receipt_count(db_session, operation_id) == 1

    revisions = (
        await db_session.execute(
            text(
                "SELECT revision, digest, calibration "
                "FROM public.sensor_calibration_revisions "
                "WHERE dataset_item_id=:id ORDER BY revision"
            ),
            {"id": item_id},
        )
    ).all()
    assert len(revisions) == 2
    assert revisions[0].revision == 1
    assert revisions[0].digest == original_digest
    assert revisions[0].calibration["intrinsic"][0] == 0.0
    assert revisions[1].revision == 2
    assert revisions[1].digest == calibration_digest(updated)


async def test_provenance_setdefault_preserves_metadata_and_fences_source_snapshots(
    db_session,
) -> None:
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"provenance-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=2,
    )
    calibration = _calibration()
    existing = DatasetItem(
        dataset_id=dataset_id,
        file_name="existing.jpg",
        file_path=f"provenance/{dataset_id}/existing.jpg",
        file_type="image",
        metadata_={
            "calibration": calibration.model_dump(mode="json"),
            "unrelated": {"keep": 1},
            "nuscenes_export": {
                "platform_calibration_digest": "f" * 64,
                "other": "keep-existing",
            },
        },
    )
    missing = DatasetItem(
        dataset_id=dataset_id,
        file_name="missing.jpg",
        file_path=f"provenance/{dataset_id}/missing.jpg",
        file_type="image",
        metadata_={
            "calibration": calibration.model_dump(mode="json"),
            "unrelated": {"keep": 2},
            "nuscenes_export": {"other": "keep-missing"},
        },
    )
    db_session.add_all([existing, missing])
    await db_session.flush()
    entries = await _provenance_entries(db_session, [existing.id, missing.id])
    operation_id = uuid.uuid4()

    reply = _spatial_result(
        await _call(
            db_session,
            _RECORD_PROVENANCE,
            {
                "operation_id": operation_id,
                "dataset_id": dataset_id,
                "entries": json.dumps(entries),
            },
        ),
        ProvenanceMutationResult,
    )
    assert reply.recorded_items == 1

    existing_metadata, missing_metadata = (
        await db_session.execute(
            text(
                "SELECT id, metadata FROM public.dataset_items "
                "WHERE id IN (:existing, :missing)"
            ),
            {"existing": existing.id, "missing": missing.id},
        )
    ).all()
    by_id = {row.id: row.metadata for row in (existing_metadata, missing_metadata)}
    digest = calibration_digest(calibration)
    assert (
        by_id[existing.id]["nuscenes_export"]["platform_calibration_digest"] == "f" * 64
    )
    assert by_id[existing.id]["nuscenes_export"]["other"] == "keep-existing"
    assert by_id[existing.id]["unrelated"] == {"keep": 1}
    assert by_id[missing.id]["nuscenes_export"]["platform_calibration_digest"] == digest
    assert by_id[missing.id]["nuscenes_export"]["other"] == "keep-missing"
    assert by_id[missing.id]["unrelated"] == {"keep": 2}
    assert (
        _json_value(
            await _call(
                db_session,
                _RECORD_PROVENANCE,
                {
                    "operation_id": operation_id,
                    "dataset_id": dataset_id,
                    "entries": json.dumps(entries),
                },
            )
        )["data"]["recorded_items"]
        == 1
    )


async def test_provenance_rejects_stale_source_and_bad_canonical_document(
    db_session,
) -> None:
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"provenance-fence-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=1,
    )
    calibration = _calibration()
    item = DatasetItem(
        dataset_id=dataset_id,
        file_name="camera.jpg",
        file_path=f"provenance/{dataset_id}/camera.jpg",
        file_type="image",
        metadata_={
            "calibration": calibration.model_dump(mode="json"),
            "nuscenes_export": {"other": "keep"},
        },
    )
    db_session.add(item)
    await db_session.flush()
    stale_entry = (await _provenance_entries(db_session, [item.id]))[0]
    item.file_path = f"provenance/{dataset_id}/changed.jpg"
    await db_session.flush()
    stale_operation = uuid.uuid4()
    stale = await _call(
        db_session,
        _RECORD_PROVENANCE,
        {
            "operation_id": stale_operation,
            "dataset_id": dataset_id,
            "entries": json.dumps([stale_entry]),
        },
    )
    _spatial_error(stale, "source_changed")

    fresh_entry = (await _provenance_entries(db_session, [item.id]))[0]
    fresh_entry["canonical_document"] = "{}"
    invalid_operation = uuid.uuid4()
    invalid = await _call(
        db_session,
        _RECORD_PROVENANCE,
        {
            "operation_id": invalid_operation,
            "dataset_id": dataset_id,
            "entries": json.dumps([fresh_entry]),
        },
    )
    _spatial_error(invalid, "camera_calibration_invalid")
    metadata = await db_session.scalar(
        text("SELECT metadata FROM public.dataset_items WHERE id=:id"),
        {"id": item.id},
    )
    assert metadata["nuscenes_export"] == {"other": "keep"}
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.dataset_spatial_operations "
                "WHERE operation_id IN (:stale, :invalid)"
            ),
            {"stale": stale_operation, "invalid": invalid_operation},
        )
        == 0
    )
