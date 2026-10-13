from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.annotation_conversion_plan import AnnotationConversionPlan
from app.db.models.annotation_operation import (
    AnnotationLineageEdge,
    AnnotationOperation,
)
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.schemas.annotation_conversion import AnnotationConversionDryRunRequest
from app.services.annotation_conversion import (
    AnnotationConversionError,
    AnnotationConversionService,
)
from app.services.raster_mask_storage import build_rle_reference


MASK_RLE = {
    "encoding": "coco_rle",
    "size": [4, 4],
    "counts": [5, 2, 2, 1, 6],
}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_task(db, owner_id: uuid.UUID, *, video: bool = False):
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-CV-{suffix}",
        name=f"conversion-{suffix}",
        data_type="video" if video else "image",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"asset-{suffix}.{'mp4' if video else 'png'}",
        file_path=f"conversion/{suffix}.{'mp4' if video else 'png'}",
        file_type="video" if video else "image",
        width=4,
        height=4,
        metadata_=(
            {"video": {"width": 4, "height": 4, "fps": 10, "frame_count": 11}}
            if video
            else {}
        ),
    )
    db.add(item)
    await db.flush()
    project = Project(
        display_id=f"P-CV-{suffix}",
        name=f"conversion-{suffix}",
        type_label="视频分割" if video else "图像分割",
        type_key="video-seg" if video else "image-seg",
        data_type="video" if video else "image",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object"}],
                "attribute_schema": {"fields": []},
            },
            "bbox": {
                "enabled": True,
                "classes": [{"name": "object"}],
                "attribute_schema": {"fields": []},
            },
        },
    )
    db.add(project)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-CV-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=item.file_type,
        status="pending",
    )
    db.add(task)
    if video:
        db.add(
            VideoSegment(
                dataset_item_id=item.id,
                segment_index=0,
                start_frame=0,
                end_frame=10,
                assignee_id=owner_id,
                locked_by=owner_id,
                lock_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
        )
    await db.flush()
    return task


async def _seed_annotation(
    db,
    task: Task,
    owner_id: uuid.UUID,
    geometry: dict,
    *,
    class_name: str = "object",
) -> Annotation:
    annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner_id,
        source="manual",
        annotation_type=geometry["type"],
        tool_unit_id=(
            "bbox" if geometry["type"] in {"bbox", "video_bbox"} else "region"
        ),
        class_name=class_name,
        geometry=geometry,
        track_id=geometry.get("track_id"),
        version=1,
    )
    db.add(annotation)
    await db.flush()
    return annotation


def _mock_mask_storage(monkeypatch, *, loaded_rle: dict = MASK_RLE) -> None:
    async def store(rle: dict):
        return build_rle_reference(rle)

    monkeypatch.setattr(
        "app.services.annotation_conversion.lock_raster_mask_references",
        AsyncMock(),
    )
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(
        "app.services.annotation_conversion.load_coco_rle",
        AsyncMock(return_value=loaded_rle),
    )
    monkeypatch.setattr(
        "app.services.annotation_conversion.prepare_mask_payload_for_write",
        AsyncMock(),
    )


async def test_polygon_mask_dry_run_does_not_store_or_reserve_content(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    payload = {
        "annotation_ids": [str(source.id)],
        "target": "mask",
        "operation": "copy",
        "scope": "image",
    }
    first = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json=payload,
        headers=_headers(token),
    )
    second = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json=payload,
        headers=_headers(token),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    store.assert_not_awaited()
    reserved = await db_session.scalar(
        select(func.count(RasterMaskUpload.id)).where(
            RasterMaskUpload.task_id == task.id
        )
    )
    assert reserved == 0

    plans = list(
        (
            await db_session.execute(
                select(AnnotationConversionPlan).order_by(
                    AnnotationConversionPlan.created_at,
                    AnnotationConversionPlan.id,
                )
            )
        ).scalars()
    )
    plans[0].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()
    from app.workers.cleanup import _expire_annotation_conversion_plans

    assert await _expire_annotation_conversion_plans(db_session) == 1
    assert await db_session.scalar(select(func.count(AnnotationConversionPlan.id))) == 1


async def test_polygon_mask_quota_is_reserved_only_during_execute(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(
        "app.services.annotation_conversion.MAX_CONVERSION_MASK_OBJECTS_PER_TASK",
        0,
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )
    assert dry_run.status_code == 200, dry_run.text

    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": dry_run.json()["plan_token"],
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
        },
        headers=_headers(token),
    )

    assert executed.status_code == 422
    assert executed.json()["detail"]["reason"] == "mask_quota_exceeded"
    store.assert_not_awaited()
    assert (
        await db_session.scalar(
            select(func.count(Annotation.id)).where(Annotation.task_id == task.id)
        )
        == 1
    )


async def test_conversion_rejects_raster_work_above_request_budget(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(
        "app.services.annotation_conversion.MAX_CONVERSION_RASTER_PIXELS",
        15,
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "reason": "conversion_budget_exceeded",
        "message": "conversion exceeds the synchronous raster pixel budget",
        "requested_pixels": 16,
        "limit_pixels": 15,
    }
    store.assert_not_awaited()


async def test_image_polygon_replace_dry_run_execute_and_replay(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {
            "type": "polygon",
            "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "holes": [[[0.25, 0.25], [0.25, 0.75], [0.75, 0.75], [0.75, 0.25]]],
        },
    )
    _mock_mask_storage(monkeypatch)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "replace",
            "scope": "image",
        },
        headers=_headers(token),
    )

    assert dry_run.status_code == 200, dry_run.text
    plan = dry_run.json()
    assert plan["items"][0]["target_type"] == "raster_mask"
    assert plan["items"][0]["source_holes"] == 1
    assert plan["items"][0]["lossy"] is False

    payload = {
        "plan_token": plan["plan_token"],
        "idempotency_key": f"convert-{uuid.uuid4().hex}",
        "confirm_replace": True,
    }
    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json=payload,
        headers=_headers(token),
    )
    replay = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json=payload,
        headers=_headers(token),
    )

    assert executed.status_code == 200, executed.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    plan_row = (
        await db_session.execute(
            select(AnnotationConversionPlan).where(
                AnnotationConversionPlan.task_id == task.id
            )
        )
    ).scalar_one()
    await db_session.delete(plan_row)
    await db_session.commit()
    replay_after_plan_cleanup = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json=payload,
        headers=_headers(token),
    )
    assert replay_after_plan_cleanup.status_code == 200
    assert replay_after_plan_cleanup.json()["idempotent_replay"] is True
    assert executed.json()["updated_annotations"][0]["id"] == str(source.id)
    assert (
        executed.json()["updated_annotations"][0]["geometry"]["type"] == "raster_mask"
    )
    operation = (
        await db_session.execute(
            select(AnnotationOperation).where(
                AnnotationOperation.id == uuid.UUID(executed.json()["operation_id"])
            )
        )
    ).scalar_one()
    assert operation.kind == "convert_annotations"
    assert (
        await db_session.scalar(
            select(func.count(AnnotationLineageEdge.id)).where(
                AnnotationLineageEdge.operation_id == operation.id
            )
        )
        == 1
    )


async def test_lossy_mask_to_bbox_requires_confirmation_and_copies_source(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "raster_mask", "mask": build_rle_reference(MASK_RLE)},
    )
    _mock_mask_storage(monkeypatch)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "bbox",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["summary"]["lossy_count"] == 1
    execute_payload = {
        "plan_token": dry_run.json()["plan_token"],
        "idempotency_key": f"convert-{uuid.uuid4().hex}",
    }
    rejected = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json=execute_payload,
        headers=_headers(token),
    )
    execute_payload["confirm_lossy"] = True
    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json=execute_payload,
        headers=_headers(token),
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["reason"] == "lossy_confirmation_required"
    assert executed.status_code == 200, executed.text
    assert executed.json()["created_annotations"][0]["geometry"]["type"] == "bbox"
    await db_session.refresh(source)
    assert source.is_active is True
    assert source.version == 1


async def test_batch_execute_rejects_snapshot_drift_without_partial_results(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    first = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]},
    )
    second = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]},
    )
    _mock_mask_storage(monkeypatch)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(first.id), str(second.id)],
            "target": "mask",
            "operation": "replace",
            "scope": "image",
        },
        headers=_headers(token),
    )
    assert dry_run.status_code == 200, dry_run.text
    second.version = 2
    await db_session.commit()

    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": dry_run.json()["plan_token"],
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
            "confirm_replace": True,
        },
        headers=_headers(token),
    )

    assert executed.status_code == 409
    assert executed.json()["detail"]["reason"] == "version_mismatch"
    assert (
        await db_session.scalar(
            select(func.count(Annotation.id)).where(Annotation.task_id == task.id)
        )
        == 2
    )


async def test_large_image_conversion_rejects_before_raster_allocation(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert item is not None
    item.width = 8192
    item.height = 8192
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    )
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "replace",
            "scope": "image",
        },
        headers=_headers(token),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "large_mask_full_scan_required"


async def test_execute_rejects_conversion_report_drift_before_storage(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )
    assert dry_run.status_code == 200, dry_run.text

    from app.services import mask_conversion

    original_conversion = mask_conversion.region_to_mask_conversion

    def drifted_conversion(geometry, width, height):
        _, metrics = original_conversion(geometry, width, height)
        return MASK_RLE, metrics

    monkeypatch.setattr(
        "app.services.annotation_conversion.region_to_mask_conversion",
        drifted_conversion,
    )
    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": dry_run.json()["plan_token"],
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
        },
        headers=_headers(token),
    )

    assert executed.status_code == 409
    assert executed.json()["detail"]["reason"] == "plan_report_mismatch"
    store.assert_not_awaited()


async def test_video_polygon_track_materializes_held_frame_only_when_explicit(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id, video=True)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {
            "type": "video_track_polygon",
            "track_id": "trk_polygon",
            "keyframes": [
                {
                    "frame_index": 0,
                    "points": [[0, 0], [0.5, 0], [0.5, 0.5]],
                    "source": "manual",
                },
                {
                    "frame_index": 10,
                    "points": [[0.5, 0.5], [1, 0.5], [1, 1]],
                    "source": "manual",
                },
            ],
            "outside": [],
        },
    )
    _mock_mask_storage(monkeypatch)
    await db_session.commit()
    base_payload = {
        "annotation_ids": [str(source.id)],
        "target": "mask",
        "operation": "copy",
        "scope": "current_frame",
        "frame_index": 5,
    }

    rejected = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json=base_payload,
        headers=_headers(token),
    )
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={**base_payload, "materialize_held": True},
        headers=_headers(token),
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["reason"] == "held_materialization_required"
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["summary"]["materialized_held_frames"] == 1
    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": accepted.json()["plan_token"],
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
        },
        headers=_headers(token),
    )
    assert executed.status_code == 200, executed.text
    created = executed.json()["created_annotations"][0]
    assert created["geometry"]["type"] == "video_track_mask"
    assert [item["frame_index"] for item in created["geometry"]["keyframes"]] == [5]
    await db_session.refresh(source)
    assert source.geometry["type"] == "video_track_polygon"
    assert source.version == 1


async def test_video_mask_replace_with_bbox_suppresses_only_current_frame(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id, video=True)
    reference = build_rle_reference(MASK_RLE)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {
            "type": "video_track_mask",
            "track_id": "trk_mask",
            "keyframes": [
                {"frame_index": 0, "mask": reference, "source": "manual"},
                {"frame_index": 10, "mask": reference, "source": "manual"},
            ],
            "outside": [],
        },
    )
    _mock_mask_storage(monkeypatch)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "bbox",
            "operation": "replace",
            "scope": "current_frame",
            "frame_index": 0,
        },
        headers=_headers(token),
    )
    assert dry_run.status_code == 200, dry_run.text
    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": dry_run.json()["plan_token"],
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
            "confirm_replace": True,
            "confirm_lossy": True,
        },
        headers=_headers(token),
    )

    assert executed.status_code == 200, executed.text
    assert executed.json()["created_annotations"][0]["geometry"]["frame_index"] == 0
    await db_session.refresh(source)
    assert [item["frame_index"] for item in source.geometry["keyframes"]] == [10]
    assert source.geometry["outside"] == [{"from": 0, "to": 0, "source": "manual"}]
    plan = await db_session.scalar(
        select(AnnotationConversionPlan).where(
            AnnotationConversionPlan.executed_operation_id
            == uuid.UUID(executed.json()["operation_id"])
        )
    )
    assert plan is not None


async def test_conversion_plan_rejects_tampered_and_expired_tokens(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "raster_mask", "mask": build_rle_reference(MASK_RLE)},
    )
    _mock_mask_storage(monkeypatch)
    await db_session.commit()

    dry_run = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "polygon",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )
    assert dry_run.status_code == 200, dry_run.text
    plan_token = dry_run.json()["plan_token"]

    tampered = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": f"{plan_token}x",
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
        },
        headers=_headers(token),
    )
    assert tampered.status_code == 404
    assert tampered.json()["detail"]["reason"] == "plan_token_invalid"

    plan = await db_session.scalar(select(AnnotationConversionPlan))
    assert plan is not None
    plan.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    expired = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": plan_token,
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
        },
        headers=_headers(token),
    )
    assert expired.status_code == 409
    assert expired.json()["detail"]["reason"] == "plan_expired"


async def test_conversion_rejects_annotation_and_task_locks_before_storage(
    httpx_client,
    db_session,
    super_admin,
    annotator,
    monkeypatch,
):
    user, token = super_admin
    other, _ = annotator
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    source.is_locked = True
    await db_session.commit()

    payload = {
        "annotation_ids": [str(source.id)],
        "target": "mask",
        "operation": "copy",
        "scope": "image",
    }
    annotation_locked = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json=payload,
        headers=_headers(token),
    )
    assert annotation_locked.status_code == 409
    assert annotation_locked.json()["detail"]["reason"] == "annotation_locked"
    store.assert_not_awaited()

    source.is_locked = False
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=other.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            unique_id=uuid.uuid4(),
        )
    )
    await db_session.commit()
    task_locked = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json=payload,
        headers=_headers(token),
    )
    assert task_locked.status_code == 409
    assert task_locked.json()["detail"]["reason"] == "task_lock_conflict"
    store.assert_not_awaited()


async def test_video_single_polygon_and_track_keyframes_convert_to_masks(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id, video=True)
    single = await _seed_annotation(
        db_session,
        task,
        user.id,
        {
            "type": "video_polygon",
            "frame_index": 3,
            "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
        },
    )
    track = await _seed_annotation(
        db_session,
        task,
        user.id,
        {
            "type": "video_track_polygon",
            "track_id": "trk_keyframes",
            "keyframes": [
                {
                    "frame_index": 0,
                    "points": [[0, 0], [0.5, 0], [0.5, 0.5]],
                    "source": "manual",
                },
                {
                    "frame_index": 10,
                    "points": [[0.5, 0.5], [1, 0.5], [1, 1]],
                    "source": "manual",
                },
            ],
            "outside": [{"from": 10, "to": 10, "source": "manual"}],
        },
    )
    _mock_mask_storage(monkeypatch)
    await db_session.commit()

    single_plan = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(single.id)],
            "target": "mask",
            "operation": "copy",
            "scope": "current_frame",
            "frame_index": 3,
        },
        headers=_headers(token),
    )
    assert single_plan.status_code == 200, single_plan.text
    assert single_plan.json()["items"][0]["frame_indexes"] == [3]

    track_plan = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(track.id)],
            "target": "mask",
            "operation": "replace",
            "scope": "keyframes",
        },
        headers=_headers(token),
    )
    assert track_plan.status_code == 200, track_plan.text
    assert track_plan.json()["items"][0]["frame_indexes"] == [0]
    executed = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:execute",
        json={
            "plan_token": track_plan.json()["plan_token"],
            "idempotency_key": f"convert-{uuid.uuid4().hex}",
            "confirm_replace": True,
        },
        headers=_headers(token),
    )
    assert executed.status_code == 200, executed.text
    converted = executed.json()["updated_annotations"][0]["geometry"]
    assert converted["type"] == "video_track_mask"
    assert [item["frame_index"] for item in converted["keyframes"]] == [0]
    assert converted["outside"] == [{"from": 10, "to": 10, "source": "manual"}]


async def test_empty_polygon_conversion_returns_validation_error_before_storage(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [0.01, 0], [0.01, 0.01]]},
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "conversion_geometry_invalid"
    store.assert_not_awaited()


async def test_video_segment_lease_is_checked_before_mask_storage(
    db_session, super_admin, reviewer, monkeypatch
):
    owner, _ = super_admin
    actor, _ = reviewer
    task = await _seed_task(db_session, owner.id, video=True)
    source = await _seed_annotation(
        db_session,
        task,
        owner.id,
        {
            "type": "video_polygon",
            "frame_index": 3,
            "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
        },
    )
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    await db_session.commit()

    with pytest.raises(AnnotationConversionError) as exc_info:
        await AnnotationConversionService(db_session).dry_run(
            task=task,
            actor=actor,
            payload=AnnotationConversionDryRunRequest(
                annotation_ids=[source.id],
                target="mask",
                operation="copy",
                scope="current_frame",
                frame_index=3,
            ),
        )

    assert exc_info.value.detail["reason"] == "segment_lock_conflict"
    store.assert_not_awaited()


async def test_polygon_to_mask_requires_project_native_mask_opt_in(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task = await _seed_task(db_session, user.id)
    source = await _seed_annotation(
        db_session,
        task,
        user.id,
        {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
    )
    project = await db_session.get(Project, task.project_id)
    assert project is not None
    project.raster_mask_native_editing_enabled = False
    _mock_mask_storage(monkeypatch)
    store = AsyncMock(return_value=build_rle_reference(MASK_RLE))
    monkeypatch.setattr("app.services.annotation_conversion.store_coco_rle", store)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotation-conversions:dry-run",
        json={
            "annotation_ids": [str(source.id)],
            "target": "mask",
            "operation": "copy",
            "scope": "image",
        },
        headers=_headers(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "raster_mask_create_disabled"
    store.assert_not_awaited()
