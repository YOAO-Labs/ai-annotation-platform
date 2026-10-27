from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene, SensorCalibrationRevision
from app.db.models.project import Project
from app.db.models.scene_track import SceneTrack
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas._jsonb_types import SensorCalibration
from app.schemas.multicamera_annotation import NormalizedCameraBbox
from app.services.exporting import packaging
from app.services.exporting.lidar_preflight import preflight_lidar_export
from app.services.multicamera_annotation import (
    MulticameraAnnotationError,
    camera_member_out,
    create_camera_member,
    delete_camera_member,
    list_camera_members,
    restore_camera_member,
    update_camera_member,
)
from app.services.scene_track_domain import bind_annotation_to_scene_track
from app.services.sensor_calibration import (
    calibration_digest,
    list_calibration_revisions,
    update_calibration,
)
from app.services.task_dataset_link import link_items
from app.workers.export import _multicamera_coco_scope_digest
from tests.factory import create_project


def _calibration(*, cx: float = 100) -> SensorCalibration:
    return SensorCalibration.model_validate(
        {
            "extrinsic": [
                0,
                -1,
                0,
                0,
                0,
                0,
                -1,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
                1,
            ],
            "intrinsic": [100, 0, cx, 0, 100, 60, 0, 0, 1],
        }
    )


async def _seed(db, owner_id: uuid.UUID):
    project = await create_project(db, owner_id=owner_id, type_key="lidar")
    project.data_type = "lidar"
    dataset = Dataset(
        display_id=f"DS-MM-{uuid.uuid4().hex[:8]}",
        name=f"multicamera-{uuid.uuid4().hex[:8]}",
        data_type="point_cloud",
        created_by=owner_id,
        metadata_={"axis_convention": "iso_8855"},
    )
    db.add(dataset)
    await db.flush()
    scene = Scene(
        display_id=f"SCN-MM-{uuid.uuid4().hex[:8]}",
        dataset_id=dataset.id,
        name=f"scene-{uuid.uuid4().hex[:8]}",
    )
    db.add(scene)
    await db.flush()
    lidar = DatasetItem(
        dataset_id=dataset.id,
        file_name="frame.pcd",
        file_path="mm/frame.pcd",
        file_type="point_cloud",
        scene_id=scene.id,
        frame_index=0,
    )
    camera = DatasetItem(
        dataset_id=dataset.id,
        file_name="front.jpg",
        file_path="mm/front.jpg",
        file_type="image",
        width=200,
        height=120,
        scene_id=scene.id,
        frame_index=0,
        metadata_={"calibration": _calibration().model_dump(mode="json")},
    )
    db.add_all([lidar, camera])
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=lidar.id,
        display_id=f"T-MM-{uuid.uuid4().hex[:8]}",
        file_name=lidar.file_name,
        file_path=lidar.file_path,
        file_type="point_cloud",
        status="in_progress",
    )
    db.add(task)
    await db.flush()
    await link_items(
        db,
        task.id,
        [
            (lidar.id, "primary_lidar", "LIDAR_TOP"),
            (camera.id, "camera_front", "CAM_FRONT"),
        ],
    )
    from tests.test_export_lidar import _register_metadata_sources

    await _register_metadata_sources(db, lidar, camera)
    source = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="box_3d",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry={
            "type": "box_3d",
            "center": [10, 0, 0],
            "size": [4, 2, 1.5],
            "rotation": [0, 0, 0],
            "convention_at_create": "iso_8855",
        },
        track_id=f"trk_{uuid.uuid4().hex}",
    )
    db.add(source)
    binding = await bind_annotation_to_scene_track(
        db,
        annotation=source,
        task=task,
        temporal_role="keyframe",
        interval_source="manual",
        actor_id=owner_id,
    )
    assert binding is not None
    await db.flush()
    return task, camera, source, binding.track


class _TrustedLidarMedia:
    """Frozen delivery double: saved media receipts, never a raw-key presign."""

    def __init__(
        self, *, project, task, links, members, calibration, receipts, expires_at
    ):
        self.project = project
        self.expires_at = expires_at
        self.links_by_task = {task.id: links}
        self.camera_members_by_task = {task.id: members}
        self.calibration_states = calibration
        self.scenes_by_id = {}
        self.poses_by_frame = {}
        self.receipts = receipts

    def service(self, db):
        # Business rows stay live for this DB-backed test; only media delivery
        # is frozen behind saved receipts.
        from app.services.exporting.service import ExportService

        return ExportService(db, media_paths={})

    def entry(self, task_id, role):
        receipt = self.receipts.get((task_id, role))
        assert receipt is not None, f"unregistered media receipt: {role}"
        return dict(receipt)


async def _task_links(db, task):
    links = {}
    for link in await db.scalars(
        select(TaskDatasetItemLink).where(TaskDatasetItemLink.task_id == task.id)
    ):
        item = await db.get(DatasetItem, link.dataset_item_id)
        if item is not None:
            links[link.role] = (link, item)
    return links


async def test_camera_member_lifecycle_and_calibration_staleness(
    db_session, super_admin
):
    user, _ = super_admin
    task, camera, source, track = await _seed(db_session, user.id)
    baseline = _calibration()
    baseline_digest = calibration_digest(baseline)

    member = await create_camera_member(
        db_session,
        task=task,
        actor_id=user.id,
        source_annotation_id=source.id,
        camera_role="camera_front",
        bbox=NormalizedCameraBbox(x=0.4, y=0.35, w=0.2, h=0.3),
        visibility="visible",
        expected_track_revision=track.revision,
        expected_calibration_revision=1,
        expected_calibration_digest=baseline_digest,
    )
    assert member.scene_track_id == source.scene_track_id
    assert member.track_id == source.track_id
    assert member.tool_unit_id == "lidar_box_3d"
    assert track.revision == 2
    current = await camera_member_out(db_session, task=task, member=member)
    assert current.relation_status == "current"
    assert current.residual is not None
    assert current.track_revision == 2
    listed, listed_revision, projected_bbox = await list_camera_members(
        db_session,
        task=task,
        scene_track_id=source.scene_track_id,
        include_inactive=False,
        projection_camera_role="camera_front",
    )
    assert [item.id for item in listed] == [member.id]
    assert listed_revision == 2
    assert projected_bbox is not None

    with pytest.raises(MulticameraAnnotationError) as duplicate:
        await create_camera_member(
            db_session,
            task=task,
            actor_id=user.id,
            source_annotation_id=source.id,
            camera_role="camera_front",
            bbox=NormalizedCameraBbox(x=0.3, y=0.3, w=0.2, h=0.2),
            visibility="visible",
            expected_track_revision=track.revision,
            expected_calibration_revision=1,
            expected_calibration_digest=baseline_digest,
        )
    assert duplicate.value.code == "camera_member_exists"

    changed = _calibration(cx=110)
    changed_state = await update_calibration(
        db_session,
        dataset_item_id=camera.id,
        calibration=changed,
        expected_revision=1,
        expected_digest=baseline_digest,
        actor_id=user.id,
    )
    assert changed_state.revision == 2
    stale = await camera_member_out(db_session, task=task, member=member)
    assert stale.relation_status == "stale"
    assert stale.bbox == current.bbox

    member = await update_camera_member(
        db_session,
        task=task,
        member_id=member.id,
        bbox=NormalizedCameraBbox(x=0.42, y=0.35, w=0.2, h=0.3),
        visibility="occluded",
        expected_version=member.version,
        expected_track_revision=track.revision,
        expected_calibration_revision=changed_state.revision,
        expected_calibration_digest=changed_state.digest,
    )
    assert member.version == 2
    assert member.sensor_visibility == "occluded"
    assert track.revision == 3
    assert (
        await camera_member_out(db_session, task=task, member=member)
    ).relation_status == "current"

    member = await delete_camera_member(
        db_session,
        task=task,
        member_id=member.id,
        expected_version=member.version,
        expected_track_revision=track.revision,
    )
    assert member.is_active is False
    assert track.revision == 4
    member = await restore_camera_member(
        db_session,
        task=task,
        member_id=member.id,
        expected_version=member.version,
        expected_track_revision=track.revision,
        expected_calibration_revision=changed_state.revision,
        expected_calibration_digest=changed_state.digest,
    )
    assert member.is_active is True
    assert track.revision == 5


async def test_multicamera_coco_preflight_allows_stale_manual_relation(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, camera, source, track = await _seed(db_session, user.id)
    baseline = _calibration()
    baseline_digest = calibration_digest(baseline)
    camera.file_size = 14
    await create_camera_member(
        db_session,
        task=task,
        actor_id=user.id,
        source_annotation_id=source.id,
        camera_role="camera_front",
        bbox=NormalizedCameraBbox(x=0.1, y=0.2, w=0.3, h=0.4),
        visibility="visible",
        expected_track_revision=track.revision,
        expected_calibration_revision=1,
        expected_calibration_digest=baseline_digest,
    )
    await update_calibration(
        db_session,
        dataset_item_id=camera.id,
        calibration=_calibration(cx=120),
        expected_revision=1,
        expected_digest=baseline_digest,
        actor_id=user.id,
    )
    monkeypatch.setattr(
        "app.services.exporting.lidar_preflight.storage_service.verify_upload",
        lambda key, bucket: {"ContentLength": 14},
    )

    report = await preflight_lidar_export(
        db_session,
        project_id=task.project_id,
        batch_id=None,
        targets=["coco-multicamera"],
        options=None,
    )

    assert report.ready is True
    assert report.checked_tasks == 1
    assert report.camera_roles == ["camera_front"]
    assert report.issues == []


async def test_multicamera_coco_preflight_rejects_unsafe_role_path(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, camera, _source, _track = await _seed(db_session, user.id)
    camera.file_size = 14
    link = await db_session.scalar(
        select(TaskDatasetItemLink).where(
            TaskDatasetItemLink.task_id == task.id,
            TaskDatasetItemLink.role == "camera_front",
        )
    )
    assert link is not None
    link.role = "camera_front/../escape"
    monkeypatch.setattr(
        "app.services.exporting.lidar_preflight.storage_service.verify_upload",
        lambda key, bucket: {"ContentLength": 14},
    )

    report = await preflight_lidar_export(
        db_session,
        project_id=task.project_id,
        batch_id=None,
        targets=["coco-multicamera"],
        options=None,
    )

    assert report.ready is False
    assert "lidar_export_archive_path_conflict" in {
        issue.code for issue in report.issues
    }


async def test_multicamera_coco_requires_trusted_media_delivery(
    db_session, super_admin
):
    user, _ = super_admin
    task, *_ = await _seed(db_session, user.id)
    with pytest.raises(ValueError, match="lidar_export_contract_missing"):
        await packaging.build_export_zip(
            db_session,
            task.project_id,
            batch_id=None,
            targets=["coco-multicamera"],
            include_attributes=True,
            video_frame_mode="keyframes",
        )


async def test_multicamera_coco_package_contains_trusted_media_contract(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, camera, source, track = await _seed(db_session, user.id)
    payload = b"trusted-image"
    primary_item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert primary_item is not None
    primary_item.file_path = ""
    digest = calibration_digest(_calibration())
    member = await create_camera_member(
        db_session,
        task=task,
        actor_id=user.id,
        source_annotation_id=source.id,
        camera_role="camera_front",
        bbox=NormalizedCameraBbox(x=0.1, y=0.2, w=0.3, h=0.4),
        visibility="visible",
        expected_track_revision=track.revision,
        expected_calibration_revision=1,
        expected_calibration_digest=digest,
    )
    project = await db_session.get(Project, task.project_id)
    assert project is not None
    sha256 = hashlib.sha256(payload).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    media = _TrustedLidarMedia(
        project=project,
        task=task,
        links=await _task_links(db_session, task),
        members=[member],
        calibration={camera.id: SimpleNamespace(revision=1, digest=digest)},
        receipts={
            (task.id, "camera_front"): {
                "presigned_url": "https://saved-export.test/owned-copy",
                "expires_at": expires_at.isoformat(),
                "sha256": sha256,
                "size_bytes": len(payload),
            }
        },
        expires_at=expires_at,
    )
    forbidden = Mock(side_effect=AssertionError("trusted delivery must not read raw"))
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", forbidden)

    zip_path, file_count, _size = await packaging.build_export_zip(
        db_session,
        task.project_id,
        batch_id=None,
        targets=["coco-multicamera"],
        include_attributes=True,
        video_frame_mode="keyframes",
        lidar_media=media,
    )
    try:
        with zipfile.ZipFile(zip_path) as archive:
            annotations = json.loads(archive.read("annotations.json"))
            manifest = json.loads(archive.read("media_manifest.json"))
            report = json.loads(archive.read("export_report.json"))
            assert "fetch_media.py" in archive.namelist()
            assert annotations["aap"]["derived_projection_fallback"] is False
            assert annotations["images"][0]["file_name"] == (
                f"images/camera_front/{task.id}/front.jpg"
            )
            assert annotations["annotations"][0]["relation_status"] == "current"
            assert manifest["contract"] == "multicamera-coco-media-v1"
            assert manifest["media"][0]["kind"] == "image"
            assert manifest["media"][0]["presigned_url"] == (
                "https://saved-export.test/owned-copy"
            )
            assert manifest["media"][0]["source_file_size"] == len(payload)
            assert manifest["media"][0]["source_sha256"] == sha256
            assert report == {
                "annotations_by_role": {"camera_front": 1},
                "derived_bbox_count": 0,
                "images": 1,
                "images_by_role": {"camera_front": 1},
                "manual_bbox_count": 1,
                "negative_images": 0,
                "stale_relation_count": 0,
            }
        assert file_count == 1
    finally:
        os.unlink(zip_path)
    forbidden.assert_not_called()


async def test_multicamera_coco_cache_digest_tracks_media_etag(
    db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    task, camera, _source, _track = await _seed(db_session, user.id)
    camera.file_size = 14
    state = {"etag": "etag-one"}
    monkeypatch.setattr(
        "app.workers.export.storage_service.verify_upload",
        lambda key, bucket: {"ContentLength": 14, "ETag": state["etag"]},
    )

    first = await _multicamera_coco_scope_digest(db_session, task.project_id, None)
    state["etag"] = "etag-two"
    second = await _multicamera_coco_scope_digest(db_session, task.project_id, None)

    assert first != second


async def test_camera_member_update_rejects_track_and_calibration_conflicts(
    db_session, super_admin
):
    user, _ = super_admin
    task, _camera, source, track = await _seed(db_session, user.id)
    digest = calibration_digest(_calibration())
    member = await create_camera_member(
        db_session,
        task=task,
        actor_id=user.id,
        source_annotation_id=source.id,
        camera_role="camera_front",
        bbox=NormalizedCameraBbox(x=0.4, y=0.35, w=0.2, h=0.3),
        visibility="visible",
        expected_track_revision=track.revision,
        expected_calibration_revision=1,
        expected_calibration_digest=digest,
    )

    with pytest.raises(MulticameraAnnotationError) as conflict:
        await update_camera_member(
            db_session,
            task=task,
            member_id=member.id,
            bbox=NormalizedCameraBbox(x=0.41, y=0.35, w=0.2, h=0.3),
            visibility=None,
            expected_version=member.version,
            expected_track_revision=track.revision - 1,
            expected_calibration_revision=1,
            expected_calibration_digest=digest,
        )
    assert conflict.value.code == "track_revision_conflict"

    current_track = await db_session.get(SceneTrack, track.id)
    assert current_track is not None
    with pytest.raises(MulticameraAnnotationError) as calibration_conflict:
        await update_camera_member(
            db_session,
            task=task,
            member_id=member.id,
            bbox=NormalizedCameraBbox(x=0.41, y=0.35, w=0.2, h=0.3),
            visibility=None,
            expected_version=member.version,
            expected_track_revision=current_track.revision,
            expected_calibration_revision=2,
            expected_calibration_digest=digest,
        )
    assert calibration_conflict.value.code == "calibration_revision_conflict"

    stored = await db_session.scalar(
        select(Annotation).where(Annotation.id == member.id)
    )
    assert stored is not None
    assert stored.version == 1


async def test_camera_member_http_create_and_list(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, _camera, source, track = await _seed(db_session, user.id)
    digest = calibration_digest(_calibration())
    headers = {"Authorization": f"Bearer {token}"}

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/point-cloud/camera-members",
        headers=headers,
        json={
            "source_annotation_id": str(source.id),
            "camera_role": "camera_front",
            "bbox": {"x": 0.4, "y": 0.35, "w": 0.2, "h": 0.3},
            "visibility": "visible",
            "expected_track_revision": track.revision,
            "expected_calibration_revision": 1,
            "expected_calibration_digest": digest,
        },
    )
    assert created.status_code == 201, created.text
    member = created.json()
    assert member["camera_role"] == "camera_front"
    assert member["track_revision"] == 2

    listed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/camera-members",
        headers=headers,
        params={
            "scene_track_id": str(source.scene_track_id),
            "projection_camera_role": "camera_front",
        },
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [item["id"] for item in body["items"]] == [member["id"]]
    assert body["track_revision"] == 2
    assert body["projected_bbox"] is not None


async def test_calibration_history_keeps_virtual_baseline_and_append_only_order(
    db_session, super_admin
):
    user, _ = super_admin
    _task, camera, _source, _track = await _seed(db_session, user.id)

    baseline = await list_calibration_revisions(db_session, camera)
    assert [item.revision for item in baseline] == [1]
    assert baseline[0].created_at is None
    assert baseline[0].digest == calibration_digest(_calibration())

    second = await update_calibration(
        db_session,
        dataset_item_id=camera.id,
        calibration=_calibration(cx=110),
        expected_revision=1,
        expected_digest=baseline[0].digest,
        actor_id=user.id,
    )
    await update_calibration(
        db_session,
        dataset_item_id=camera.id,
        calibration=_calibration(cx=120),
        expected_revision=second.revision,
        expected_digest=second.digest,
        actor_id=user.id,
    )

    history = await list_calibration_revisions(db_session, camera)
    assert [item.revision for item in history] == [3, 2, 1]
    assert [item.calibration.intrinsic[2] for item in history] == [120, 110, 100]
    assert all(item.created_at is not None for item in history)


async def test_virtual_calibration_history_uses_one_consistent_read():
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        file_name="front.jpg",
        file_path="mm/front.jpg",
        file_type="image",
        metadata_={"calibration": _calibration().model_dump(mode="json")},
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = []
    db.execute.return_value = result

    history = await list_calibration_revisions(db, item)

    assert [entry.revision for entry in history] == [1]
    assert history[0].created_at is None
    db.execute.assert_awaited_once()


async def test_camera_calibration_history_http_and_noop_update(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    task, camera, _source, _track = await _seed(db_session, user.id)
    headers = {"Authorization": f"Bearer {token}"}
    digest = calibration_digest(_calibration())
    path = f"/api/v1/tasks/{task.id}/point-cloud/cameras/camera_front/calibration"
    camera.width = None
    camera.height = None
    await db_session.flush()

    initial = await httpx_client.get(path, headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["current_revision"] == 1
    assert [item["revision"] for item in initial.json()["items"]] == [1]

    unchanged = await httpx_client.patch(
        path,
        headers=headers,
        json={
            "calibration": _calibration().model_dump(mode="json"),
            "expected_revision": 1,
            "expected_digest": digest,
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["revision"] == 1
    materialized = await db_session.scalar(
        select(SensorCalibrationRevision.id).where(
            SensorCalibrationRevision.dataset_item_id == camera.id
        )
    )
    assert materialized is None

    changed = await httpx_client.patch(
        path,
        headers=headers,
        json={
            "calibration": _calibration(cx=110).model_dump(mode="json"),
            "expected_revision": 1,
            "expected_digest": digest,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2

    history = await httpx_client.get(path, headers=headers)
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["current_revision"] == 2
    assert [item["revision"] for item in body["items"]] == [2, 1]
    assert str(camera.id) == body["items"][0]["dataset_item_id"]


async def test_project_owner_cannot_update_cross_project_shared_calibration(
    db_session, httpx_client, project_admin, super_admin
):
    owner, token = project_admin
    other_owner, _ = super_admin
    task, camera, _source, _track = await _seed(db_session, owner.id)
    other_project = await create_project(
        db_session, owner_id=other_owner.id, type_key="lidar"
    )
    other_project.data_type = "lidar"
    other_task = Task(
        project_id=other_project.id,
        dataset_item_id=task.dataset_item_id,
        display_id=f"T-MM-{uuid.uuid4().hex[:8]}",
        file_name=task.file_name,
        file_path=task.file_path,
        file_type="point_cloud",
        status="in_progress",
    )
    db_session.add(other_task)
    await db_session.flush()
    db_session.add(
        TaskDatasetItemLink(
            task_id=other_task.id,
            dataset_item_id=camera.id,
            role="camera_front",
            sensor_name="CAM_FRONT",
        )
    )
    await db_session.flush()

    response = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/point-cloud/cameras/camera_front/calibration",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "calibration": _calibration(cx=110).model_dump(mode="json"),
            "expected_revision": 1,
            "expected_digest": calibration_digest(_calibration()),
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == (
        "Only a super admin can update calibration shared across projects"
    )
    assert (
        await db_session.scalar(
            select(SensorCalibrationRevision.id).where(
                SensorCalibrationRevision.dataset_item_id == camera.id
            )
        )
        is None
    )
