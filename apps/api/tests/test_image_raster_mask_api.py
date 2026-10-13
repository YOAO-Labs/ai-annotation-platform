from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from app.config import Settings, settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.raster_mask_storage import build_rle_reference
from tests.factory import create_membership


FOREGROUND_RLE = {
    "encoding": "coco_rle",
    "size": [2, 3],
    "counts": [1, 2, 3],
}
EMPTY_RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [6]}


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


async def _seed_image_mask(
    db,
    *,
    owner_id: uuid.UUID,
    user_id: uuid.UUID,
    batch_annotator_id: uuid.UUID | None = None,
) -> tuple[Task, Annotation]:
    suffix = uuid.uuid4().hex[:8]
    dataset = Dataset(
        display_id=f"DS-RM-{suffix}",
        name=f"raster-{suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="mask.png",
        file_path=f"images/{suffix}.png",
        file_type="image",
        width=3,
        height=2,
    )
    db.add(item)
    await db.flush()
    project = Project(
        display_id=f"P-RM-{suffix}",
        name=f"raster-{suffix}",
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
    if user_id != owner_id:
        await create_membership(
            db,
            project_id=project.id,
            user_id=user_id,
            role="annotator",
            assigned_by=owner_id,
        )
    batch = None
    if batch_annotator_id is not None:
        batch = TaskBatch(
            project_id=project.id,
            display_id=f"B-RM-{suffix}",
            name="mask batch",
            status="active",
            annotator_id=batch_annotator_id,
            assigned_user_ids=[str(batch_annotator_id)],
        )
        db.add(batch)
        await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        batch_id=batch.id if batch else None,
        display_id=f"T-RM-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": build_rle_reference(FOREGROUND_RLE)},
    )
    db.add(annotation)
    await db.flush()
    return task, annotation


async def test_image_mask_content_upload_checks_write_gate_before_storage(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    task, _annotation = await _seed_image_mask(
        db_session,
        owner_id=user.id,
        user_id=user.id,
    )
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    reserve = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr("app.api.v1.annotations.reserve_raster_mask_upload", reserve)
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle", store)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/mask-content",
        json=FOREGROUND_RLE,
        headers=_headers(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "raster_mask_create_disabled"
    reserve.assert_not_awaited()
    store.assert_not_awaited()


def test_raster_mask_rollout_flag_defaults_and_env(monkeypatch):
    assert Settings.model_fields["raster_mask_read_enabled"].default is True
    assert Settings.model_fields["raster_mask_create_enabled"].default is True
    monkeypatch.setenv("RASTER_MASK_READ_ENABLED", "false")
    monkeypatch.setenv("RASTER_MASK_CREATE_ENABLED", "true")
    configured = Settings(_env_file=None)
    assert configured.raster_mask_read_enabled is False
    assert configured.raster_mask_create_enabled is True


def test_static_mask_openapi_has_typed_payload_and_304():
    from app.main import app

    operation = app.openapi()["paths"][
        "/api/v1/annotations/{annotation_id}/mask-content"
    ]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CocoRleContent"
    }
    assert operation["responses"]["304"]["description"] == "Not Modified"


async def test_static_mask_get_supports_etag_and_frame_alias(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    _task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.api.v1.annotations.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    await db_session.commit()

    first = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token),
    )
    assert first.status_code == 200
    assert first.json() == FOREGROUND_RLE
    assert first.headers["cache-control"] == "private, no-cache"
    etag = first.headers["etag"]

    cached = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token, **{"If-None-Match": f"W/{etag}"}),
    )
    alias = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content/99",
        headers=_headers(token, **{"If-None-Match": etag}),
    )
    assert cached.status_code == 304
    assert alias.status_code == 304
    assert cached.headers["cache-control"] == "private, no-cache"
    assert alias.headers["cache-control"] == "private, no-cache"
    load.assert_awaited_once()

    # A stable annotation URL can point at new pixels after edits or restore.
    annotation.geometry = {
        "type": "raster_mask",
        "mask": build_rle_reference(EMPTY_RLE),
    }
    load.return_value = EMPTY_RLE
    await db_session.commit()
    for suffix in ("", "/99"):
        changed = await httpx_client.get(
            f"/api/v1/annotations/{annotation.id}/mask-content{suffix}",
            headers=_headers(token, **{"If-None-Match": etag}),
        )
        assert changed.status_code == 200
        assert changed.json() == EMPTY_RLE
        assert changed.headers["etag"] != etag
        assert changed.headers["cache-control"] == "private, no-cache"


async def test_single_frame_video_mask_get_is_bound_to_its_frame(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    project = await db_session.get(Project, task.project_id)
    assert item is not None and project is not None
    item.file_type = "video"
    item.metadata_ = {"video": {"frame_count": 10}}
    task.file_type = "video"
    project.data_type = "video"
    annotation.annotation_type = "video_mask"
    annotation.geometry = {
        "type": "video_mask",
        "frame_index": 4,
        "mask": build_rle_reference(FOREGROUND_RLE),
    }
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.api.v1.annotations.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    await db_session.commit()

    static = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token),
    )
    exact = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content/4",
        headers=_headers(token),
    )
    outside = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content/5",
        headers=_headers(token),
    )

    assert static.status_code == 200
    assert exact.status_code == 200
    assert outside.status_code == 404
    assert outside.json()["detail"] == "mask is outside at this frame"
    assert load.await_count == 2


async def test_static_mask_get_returns_retryable_structured_corruption(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    _task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    monkeypatch.setattr(
        "app.api.v1.annotations.load_coco_rle",
        AsyncMock(side_effect=ValueError("stored mask RLE digest mismatch")),
    )
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "reason": "digest_mismatch",
        "retryable": True,
        "message": "mask object is invalid: stored mask RLE digest mismatch",
    }


async def test_static_mask_get_returns_structured_storage_unavailable(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    _task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    monkeypatch.setattr(
        "app.api.v1.annotations.load_coco_rle",
        AsyncMock(side_effect=RuntimeError("object storage offline")),
    )
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "reason": "mask_storage_unavailable",
        "retryable": True,
        "message": "Mask object storage is unavailable",
    }


async def test_mask_get_enforces_task_assignment(
    httpx_client, db_session, super_admin, annotator, monkeypatch
):
    owner, _ = super_admin
    user, token = annotator
    _task, annotation = await _seed_image_mask(
        db_session,
        owner_id=owner.id,
        user_id=user.id,
        batch_annotator_id=owner.id,
    )
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.api.v1.annotations.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token),
    )
    assert response.status_code == 404
    load.assert_not_awaited()


async def test_raster_mask_read_flag_fails_closed(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    _task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.api.v1.annotations.load_coco_rle", load)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", False)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/annotations/{annotation.id}/mask-content",
        headers=_headers(token),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "raster_mask_read_disabled"
    load.assert_not_awaited()


async def test_raster_mask_create_flag_blocks_post_and_patch(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    await db_session.commit()
    payload = {
        "annotation_type": "raster_mask",
        "tool_unit_id": "region",
        "class_name": "object",
        "geometry": annotation.geometry,
    }

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=payload,
        headers=_headers(token),
    )
    patched = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={"geometry": annotation.geometry},
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )
    assert created.status_code == 409
    assert patched.status_code == 409
    assert created.json()["detail"]["reason"] == "raster_mask_create_disabled"
    assert patched.json()["detail"]["reason"] == "raster_mask_create_disabled"


async def test_raster_mask_write_maps_size_and_empty_foreground_errors(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    load = AsyncMock(return_value=EMPTY_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    await db_session.commit()

    wrong_size = {
        **annotation.geometry,
        "mask": {**annotation.geometry["mask"], "size": [3, 2]},
    }
    mismatch = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json={
            "annotation_type": "raster_mask",
            "tool_unit_id": "region",
            "class_name": "object",
            "geometry": wrong_size,
        },
        headers=_headers(token),
    )
    empty = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json={
            "annotation_type": "raster_mask",
            "tool_unit_id": "region",
            "class_name": "object",
            "geometry": annotation.geometry,
        },
        headers=_headers(token),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["reason"] == "raster_mask_size_mismatch"
    assert empty.status_code == 422
    assert empty.json()["detail"]["reason"] == "raster_mask_empty_foreground"


async def test_raster_mask_replacement_requires_if_match(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={"geometry": annotation.geometry},
        headers=_headers(token),
    )

    assert response.status_code == 428
    assert response.json()["detail"]["reason"] == "if_match_required"


async def test_invalid_mask_type_transition_rejects_before_storage_io(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    prepare = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.tasks.annotations.prepare_mask_geometry_for_annotation_write",
        prepare,
    )
    await db_session.commit()

    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={
            "geometry": {
                "type": "video_track_mask",
                "track_id": "track-1",
                "keyframes": [{"frame_index": 0, "mask": annotation.geometry["mask"]}],
            }
        },
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "reason": "geometry_type_transition_not_allowed",
        "from_type": "raster_mask",
        "to_type": "video_track_mask",
    }
    prepare.assert_not_awaited()


async def test_locked_annotation_rejects_geometry_patch(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    annotation.is_locked = True
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    await db_session.commit()

    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={"geometry": annotation.geometry},
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "annotation_locked"


async def test_locked_annotation_rejects_delete(httpx_client, db_session, super_admin):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    annotation.is_locked = True
    await db_session.commit()

    response = await httpx_client.delete(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        headers=_headers(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "annotation_locked"
    await db_session.refresh(annotation)
    assert annotation.is_active is True


async def test_raster_mask_to_polygon_syncs_type_and_allows_gate_off(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    monkeypatch.setattr(settings, "raster_mask_create_enabled", False)
    await db_session.commit()

    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={
            "geometry": {
                "type": "polygon",
                "points": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8]],
            }
        },
        headers=_headers(token, **{"If-Match": 'W/"1"'}),
    )

    assert response.status_code == 200, response.text
    assert response.json()["annotation_type"] == "polygon"
    assert response.json()["geometry"]["type"] == "polygon"
    assert response.headers["etag"] == 'W/"2"'


async def test_polygon_to_raster_syncs_type_and_rejects_stale_version(
    httpx_client, db_session, super_admin, monkeypatch
):
    user, token = super_admin
    task, annotation = await _seed_image_mask(
        db_session, owner_id=user.id, user_id=user.id
    )
    annotation.annotation_type = "polygon"
    annotation.geometry = {
        "type": "polygon",
        "points": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8]],
    }
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    load = AsyncMock(return_value=FOREGROUND_RLE)
    monkeypatch.setattr("app.services.raster_mask_storage.load_coco_rle", load)
    await db_session.commit()
    await db_session.refresh(annotation)
    current_version = annotation.version

    stale = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={
            "geometry": {
                "type": "raster_mask",
                "mask": build_rle_reference(FOREGROUND_RLE),
            }
        },
        headers=_headers(token, **{"If-Match": f'W/"{current_version - 1}"'}),
    )
    converted = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation.id}",
        json={
            "geometry": {
                "type": "raster_mask",
                "mask": build_rle_reference(FOREGROUND_RLE),
            }
        },
        headers=_headers(token, **{"If-Match": f'W/"{current_version}"'}),
    )

    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "reason": "version_mismatch",
        "current_version": current_version,
    }
    assert converted.status_code == 200, converted.text
    assert converted.json()["annotation_type"] == "raster_mask"
    assert converted.json()["geometry"]["type"] == "raster_mask"
