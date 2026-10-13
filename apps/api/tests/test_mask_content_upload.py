"""v0.23.5 · WS-D · D3 · mask-content upload quota + async store.

``upload_task_mask_content`` returns an anonymous content-addressed reference
(not yet linked to an annotation), so the per-annotation ``is_locked`` guard
doesn't apply at upload time. Instead we enforce a per-task quota on the
number of distinct ``raster-masks/sha256/...`` object keys already referenced
by the task's annotations + predictions, rejecting with 422 once the cap is
crossed.

Also covers the D1 routing: ``encoding == "coco_rle_gzip"`` is stored via the
gzip path; absent ``encoding`` falls through to the legacy uncompressed path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.api.v1.annotations import (
    MAX_MASK_OBJECTS_PER_TASK,
    _count_task_mask_references,
)
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.project import Project
from app.db.models.task import Task
from app.config import settings

RLE = {"encoding": "coco_rle", "size": [2, 3], "counts": [1, 2, 2, 1]}
# Distinct object_key → distinct counted reference.
_KEY_FMT = "raster-masks/sha256/{ab}/{cd}/{digest}.json"


@pytest.fixture(autouse=True)
def _enable_native_mask_writes(monkeypatch):
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)


def _mask_ref(digest: str = "a" * 64) -> dict:
    ab, cd = digest[:2], digest[2:4]
    return {
        "encoding": "coco_rle_ref",
        "size": [2, 3],
        "object_key": _KEY_FMT.format(ab=ab, cd=cd, digest=digest),
        "sha256": digest,
        "runs": 4,
        "bytes": 55,
    }


async def _seed_task(db, owner_id):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-MCU-{suffix}",
        name=f"mcu-{suffix}",
        type_label="image",
        type_key="image-bbox",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={"region": {"enabled": True, "classes": []}},
    )
    db.add(proj)
    await db.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id=f"T-MCU-{suffix}",
        file_name="x.jpg",
        file_path="images/x.jpg",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return proj, task


async def test_count_task_mask_references_counts_distinct_object_keys(
    db_session, super_admin
):
    """Only task-owned uploads that have not been linked consume quota."""
    user, _ = super_admin
    _proj, task = await _seed_task(db_session, user.id)
    ref_a = _mask_ref("a" * 64)
    ref_b = _mask_ref("b" * 64)
    db_session.add_all(
        [
            RasterMaskUpload(
                task_id=task.id,
                object_key=ref_a["object_key"],
            ),
            RasterMaskUpload(
                task_id=task.id,
                object_key=ref_b["object_key"],
                linked_at=datetime.now(timezone.utc),
            ),
        ]
    )
    await db_session.flush()
    count = await _count_task_mask_references(db_session, task.id)
    assert count == 1


async def test_count_task_mask_references_isolated_per_task(db_session, super_admin):
    """other tasks' references don't leak into this task's count."""
    user, _ = super_admin
    _proj, task_a = await _seed_task(db_session, user.id)
    _proj2, task_b = await _seed_task(db_session, user.id)
    db_session.add(
        RasterMaskUpload(
            task_id=task_a.id,
            object_key=_mask_ref("c" * 64)["object_key"],
        )
    )
    await db_session.flush()
    assert await _count_task_mask_references(db_session, task_a.id) == 1
    assert await _count_task_mask_references(db_session, task_b.id) == 0


async def test_upload_task_mask_content_enforces_quota(
    httpx_client, super_admin, db_session, monkeypatch
):
    """pre-seed task with MAX_MASK_OBJECTS_PER_TASK refs → next upload 422."""
    user, token = super_admin
    _proj, task = await _seed_task(db_session, user.id)

    # Stub the counter so we don't have to materialize 2048 annotation rows.
    async def _full_count(db, task_id):
        assert task_id == task.id
        return MAX_MASK_OBJECTS_PER_TASK

    monkeypatch.setattr(
        "app.api.v1.annotations._count_task_mask_references", _full_count
    )
    monkeypatch.setattr(
        "app.api.v1.annotations.reserve_raster_mask_upload",
        AsyncMock(side_effect=ValueError("quota")),
    )
    # Storage should never be hit on a rejected upload.
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle", AsyncMock())
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle_gzip", AsyncMock())
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/mask-content",
        json=RLE,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["reason"] == "mask_quota_exceeded"
    assert body["limit"] == MAX_MASK_OBJECTS_PER_TASK


async def test_upload_task_mask_content_routes_gzip_when_encoding_declared(
    httpx_client, super_admin, db_session, monkeypatch
):
    """payload with encoding=coco_rle_gzip → store_coco_rle_gzip (D1 routing)."""
    user, token = super_admin
    _proj, task = await _seed_task(db_session, user.id)

    from app.services.raster_mask_storage import build_rle_gzip_reference

    expected = build_rle_gzip_reference(RLE)
    gzip_store = AsyncMock(return_value=expected)
    json_store = AsyncMock(
        return_value={
            "encoding": "coco_rle_ref",
            "object_key": "raster-masks/sha256/ab/cd/" + "b" * 64 + ".json",
        }
    )
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle_gzip", gzip_store)
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle", json_store)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/mask-content",
        json={**RLE, "encoding": "coco_rle_gzip"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    gzip_store.assert_awaited_once()
    json_store.assert_not_awaited()
    assert resp.json()["encoding"] == "coco_rle_ref"
    assert resp.json()["storage_encoding"] == "gzip"


async def test_upload_task_mask_content_defaults_to_json_path(
    httpx_client, super_admin, db_session, monkeypatch
):
    """payload without encoding → legacy uncompressed JSON store (backward compat)."""
    user, token = super_admin
    _proj, task = await _seed_task(db_session, user.id)

    from app.services.raster_mask_storage import build_rle_reference

    json_store = AsyncMock(return_value=build_rle_reference(RLE))
    gzip_store = AsyncMock()
    content_lock = AsyncMock()
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle", json_store)
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle_gzip", gzip_store)
    monkeypatch.setattr(
        "app.api.v1.annotations.lock_raster_mask_references", content_lock
    )
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/mask-content",
        json=RLE,  # no encoding field → legacy path
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    json_store.assert_awaited_once()
    gzip_store.assert_not_awaited()
    content_lock.assert_awaited_once_with(
        db_session,
        build_rle_reference(RLE),
        verify=False,
    )
    assert resp.json()["encoding"] == "coco_rle_ref"


async def test_upload_task_mask_content_accepts_8k_image_envelope(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    _proj, task = await _seed_task(db_session, user.id)
    rle = {"encoding": "coco_rle", "size": [8192, 8192], "counts": [67_108_864]}

    from app.services.raster_mask_storage import build_rle_reference

    expected = build_rle_reference(rle)
    store = AsyncMock(return_value=expected)
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle", store)
    monkeypatch.setattr(
        "app.api.v1.annotations.lock_raster_mask_references", AsyncMock()
    )
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/mask-content",
        json=rle,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["size"] == [8192, 8192]
    store.assert_awaited_once()


async def test_upload_task_mask_content_keeps_video_at_4k(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    _proj, task = await _seed_task(db_session, user.id)
    task.file_type = "video"
    store = AsyncMock()
    monkeypatch.setattr("app.api.v1.annotations.store_coco_rle", store)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/mask-content",
        json={"encoding": "coco_rle", "size": [1, 4097], "counts": [4097]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "video_mask_dimensions_exceeded"
    store.assert_not_awaited()


# Note (D3): the per-annotation ``is_locked`` check does NOT apply at upload
# time because the endpoint returns an anonymous reference not yet linked to
# any annotation. ``_assert_task_editable`` still runs (task-level review /
# completed lock), but annotation-level locks are enforced on the POST/PATCH
# that links the reference, not on the content upload.
