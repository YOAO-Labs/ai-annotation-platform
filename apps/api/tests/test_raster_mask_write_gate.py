from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.services.annotation import AnnotationService
from app.services.prediction import PredictionService
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    build_rle_reference,
)


FOREGROUND_RLE = {
    "encoding": "coco_rle",
    "size": [2, 3],
    "counts": [1, 2, 3],
}
EMPTY_RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [6]}


async def _seed_image_task(db, owner_id: uuid.UUID, *, item_type: str = "image"):
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-WG-{suffix}",
        name=f"write-gate-{suffix}",
        data_type=item_type,
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"mask-{suffix}.png",
        file_path=f"write-gate/{suffix}.png",
        file_type=item_type,
        width=3,
        height=2,
    )
    db.add(item)
    await db.flush()
    project = Project(
        display_id=f"P-WG-{suffix}",
        name=f"write-gate-{suffix}",
        type_label="图像分割",
        type_key="image-seg",
        data_type="image",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object"}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-WG-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=item_type,
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, task


def _shape(reference: dict) -> dict:
    return {
        "type": "raster_mask",
        "tool_unit_id": "region",
        "class_name": "object",
        "geometry": {"type": "raster_mask", "mask": reference},
        "confidence": 0.9,
    }


async def _seed_upload(db, task_id: uuid.UUID, reference: dict):
    upload = RasterMaskUpload(
        task_id=task_id,
        object_key=reference["object_key"],
    )
    db.add(upload)
    await db.flush()
    return upload


async def _seed_prediction(db, project: Project, task: Task, reference: dict):
    prediction = Prediction(
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        tool_unit_id="region",
        result=[_shape(reference)],
        source="ml_backend",
    )
    db.add(prediction)
    await db.flush()
    return prediction


async def _count_for_task(db, model, task_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(model).where(model.task_id == task_id)
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_prediction_create_gate_off_rejects_before_flush_or_link(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(FOREGROUND_RLE)
    upload = await _seed_upload(db_session, task.id, reference)
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)

    with pytest.raises(RasterMaskContractError) as exc_info:
        await PredictionService(db_session).create_from_ml_result(
            task_id=task.id,
            project_id=project.id,
            ml_backend_id=None,
            result=[_shape(reference)],
        )

    assert exc_info.value.detail["reason"] == "raster_mask_create_disabled"
    assert await _count_for_task(db_session, Prediction, task.id) == 0
    await db_session.refresh(upload)
    await db_session.refresh(task)
    assert upload.linked_at is None
    assert task.total_predictions == 0
    load.assert_not_awaited()


@pytest.mark.parametrize("disabled_layer", ["read", "project", "region"])
@pytest.mark.asyncio
async def test_prediction_create_honors_every_effective_rollout_layer(
    disabled_layer, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(FOREGROUND_RLE)
    upload = await _seed_upload(db_session, task.id, reference)
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    if disabled_layer == "read":
        monkeypatch.setattr(settings, "raster_mask_read_enabled", False)
    elif disabled_layer == "project":
        project.raster_mask_native_editing_enabled = False
    else:
        project.tool_bindings = {
            **project.tool_bindings,
            "region": {**project.tool_bindings["region"], "enabled": False},
        }
    await db_session.flush()

    with pytest.raises(RasterMaskContractError) as exc_info:
        await PredictionService(db_session).create_from_ml_result(
            task_id=task.id,
            project_id=project.id,
            ml_backend_id=None,
            result=[_shape(reference)],
        )

    assert exc_info.value.detail["reason"] == "raster_mask_create_disabled"
    assert await _count_for_task(db_session, Prediction, task.id) == 0
    await db_session.refresh(upload)
    assert upload.linked_at is None
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_prediction_create_gate_on_validates_then_links(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(FOREGROUND_RLE)
    upload = await _seed_upload(db_session, task.id, reference)
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    prediction = await PredictionService(db_session).create_from_ml_result(
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        result=[_shape(reference)],
    )

    await db_session.refresh(upload)
    assert prediction.result[0]["geometry"]["type"] == "raster_mask"
    assert upload.linked_at is not None
    load.assert_awaited_once_with(reference)


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("media", "raster_mask_image_required"),
        ("size", "raster_mask_size_mismatch"),
        ("empty", "raster_mask_empty_foreground"),
        ("reference", "mask_reference_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_prediction_create_gate_on_keeps_all_contract_validation(
    case, expected_reason, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(
        db_session,
        user.id,
        item_type="video" if case == "media" else "image",
    )
    rle = EMPTY_RLE if case == "empty" else FOREGROUND_RLE
    reference = build_rle_reference(rle)
    if case == "size":
        reference = {**reference, "size": [3, 2]}
    upload = await _seed_upload(db_session, task.id, reference)
    if case == "reference":
        load = AsyncMock(side_effect=ValueError("digest mismatch"))
    else:
        load = AsyncMock(return_value=rle)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    with pytest.raises(RasterMaskContractError) as exc_info:
        await PredictionService(db_session).create_from_ml_result(
            task_id=task.id,
            project_id=project.id,
            ml_backend_id=None,
            result=[_shape(reference)],
        )

    assert exc_info.value.detail["reason"] == expected_reason
    assert await _count_for_task(db_session, Prediction, task.id) == 0
    await db_session.refresh(upload)
    assert upload.linked_at is None


@pytest.mark.asyncio
async def test_accept_prediction_gate_off_rejects_before_annotation_or_link(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(FOREGROUND_RLE)
    prediction = await _seed_prediction(db_session, project, task, reference)
    upload = await _seed_upload(db_session, task.id, reference)
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/accept",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "raster_mask_create_disabled"
    assert await _count_for_task(db_session, Annotation, task.id) == 0
    await db_session.refresh(upload)
    assert upload.linked_at is None
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_prediction_gate_on_validates_links_and_flushes_annotation(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(FOREGROUND_RLE)
    prediction = await _seed_prediction(db_session, project, task, reference)
    upload = await _seed_upload(db_session, task.id, reference)
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    annotations = await AnnotationService(db_session).accept_prediction(
        prediction.id, user.id
    )

    await db_session.refresh(upload)
    assert annotations is not None
    assert len(annotations) == 1
    assert annotations[0].geometry == {"type": "raster_mask", "mask": reference}
    assert upload.linked_at is not None
    load.assert_awaited_once_with(reference)


@pytest.mark.asyncio
async def test_accept_prediction_gate_on_validation_failure_leaves_no_annotation_or_link(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(EMPTY_RLE)
    prediction = await _seed_prediction(db_session, project, task, reference)
    upload = await _seed_upload(db_session, task.id, reference)
    load = AsyncMock(return_value=EMPTY_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

    with pytest.raises(RasterMaskContractError) as exc_info:
        await AnnotationService(db_session).accept_prediction(prediction.id, user.id)

    assert exc_info.value.detail["reason"] == "raster_mask_empty_foreground"
    assert await _count_for_task(db_session, Annotation, task.id) == 0
    await db_session.refresh(upload)
    assert upload.linked_at is None


@pytest.mark.asyncio
async def test_gate_off_allows_metadata_patch_transition_out_and_delete(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    project, task = await _seed_image_task(db_session, user.id)
    reference = build_rle_reference(FOREGROUND_RLE)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": reference},
    )
    db_session.add(annotation)
    await db_session.flush()
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    service = AnnotationService(db_session)

    updated = await service.update(annotation.id, attributes={"reviewed": True})
    transitioned = await service.update(
        annotation.id,
        geometry={
            "type": "polygon",
            "points": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8]],
        },
    )
    deleted = await service.delete(annotation.id)

    assert updated is not None and updated.attributes == {"reviewed": True}
    assert transitioned is not None and transitioned.geometry["type"] == "polygon"
    assert deleted is True
    assert annotation.is_active is False
    load.assert_not_awaited()
