import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.errors import ConflictError
from ai_annotation.point_cloud import PointCloud
from ai_annotation.point_cloud_models import (
    CameraAnnotationMember,
    CameraAnnotationMemberCreate,
    CameraAnnotationMemberDelete,
    CameraAnnotationMemberList,
    CameraAnnotationMemberRestore,
    CameraAnnotationMemberUpdate,
    NormalizedCameraBbox,
    PointCloudManifest,
    SensorCalibration,
    SensorCalibrationHistory,
    SensorCalibrationRevision,
    SensorCalibrationUpdate,
)

from .conftest import API

TASK_ID = str(uuid4())
MEMBER_ID = str(uuid4())
SOURCE_ANNOTATION_ID = str(uuid4())
TRACK_ID = str(uuid4())
DATASET_ITEM_ID = str(uuid4())
SCENE_ID = str(uuid4())
CALIBRATION_DIGEST = "a" * 64
CURRENT_CALIBRATION_DIGEST = "b" * 64


def _calibration(**extra):
    return {
        "extrinsic": [float(index) for index in range(16)],
        "intrinsic": [float(index) for index in range(9)],
        "rect": None,
        **extra,
    }


def _member(**changes):
    return {
        "id": MEMBER_ID,
        "task_id": TASK_ID,
        "scene_track_id": TRACK_ID,
        "track_id": "trk_abc",
        "class_name": "vehicle",
        "camera_dataset_item_id": DATASET_ITEM_ID,
        "camera_role": "camera_front",
        "bbox": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3},
        "visibility": "visible",
        "version": 2,
        "is_active": True,
        "calibration_revision": 1,
        "calibration_digest": CALIBRATION_DIGEST,
        "current_calibration_revision": 2,
        "current_calibration_digest": CURRENT_CALIBRATION_DIGEST,
        "relation_status": "stale",
        "track_revision": 8,
        "residual": {
            "iou": 0.8,
            "max_edge_residual_px": 2,
            "mean_edge_residual_px": 1,
            "max_edge_residual_ratio": 0.01,
            "projected_bbox": {"x": 0.12, "y": 0.2, "w": 0.38, "h": 0.3},
        },
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:01:00Z",
        "future_field": {"preserved": True},
        **changes,
    }


def _revision(**changes):
    return {
        "dataset_item_id": DATASET_ITEM_ID,
        "revision": 2,
        "digest": CURRENT_CALIBRATION_DIGEST,
        "calibration": _calibration(future_calibration_mode="stereo-v2"),
        "created_at": "2026-10-16T00:00:00Z",
        **changes,
    }


def test_point_cloud_manifest_returns_signed_urls_and_additive_camera_data(
    client, respx_mock
):
    route = respx_mock.get(f"{API}/tasks/{TASK_ID}/point-cloud/manifest").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "point_cloud_url": "https://objects.example/pcd?signature=short-lived",
                "point_cloud_format": "pcd",
                "cameras": [
                    {
                        "name": "front",
                        "role": "camera_front",
                        "dataset_item_id": DATASET_ITEM_ID,
                        "image_url": "https://objects.example/front.jpg?signature=short-lived",
                        "width": 1920,
                        "height": 1080,
                        "calibration": _calibration(
                            future_calibration_mode="stereo-v2"
                        ),
                        "calibration_revision": 2,
                        "calibration_digest": CURRENT_CALIBRATION_DIGEST,
                        "future_camera_flag": "preserved",
                    }
                ],
                "expires_in": 3600,
                "axis_convention": "ros_rep103",
                "scene_id": SCENE_ID,
                "scene_name": "street",
                "frame_index": 4,
                "scene_total_frames": 8,
                "ego_pose": {
                    "frame_index": 4,
                    "timestamp_us": 123,
                    "ego_translation": [0, 0, 0],
                    "ego_rotation": [1, 0, 0, 0],
                    "source_metadata": {},
                },
                "future_manifest_flag": True,
            },
        )
    )

    result = PointCloud(client._http).manifest(TASK_ID)

    assert isinstance(result, PointCloudManifest)
    assert result.point_cloud_url.endswith("signature=short-lived")
    assert "signature=short-lived" not in repr(result)
    assert result.expires_in == 3600
    assert result.cameras[0].calibration.intrinsic[0] == 0
    assert result.cameras[0].calibration.future_calibration_mode == "stereo-v2"
    assert result.cameras[0].future_camera_flag == "preserved"
    assert result.ego_pose.frame_index == 4
    assert result.future_manifest_flag is True
    assert route.called


def test_camera_members_list_filters_and_preserves_stale_calibration_relation(
    client, respx_mock
):
    route = respx_mock.get(f"{API}/tasks/{TASK_ID}/point-cloud/camera-members").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [_member()],
                "track_revision": 8,
                "projected_bbox": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3},
            },
        )
    )

    result = PointCloud(client._http).camera_members(
        TASK_ID,
        scene_track_id=TRACK_ID,
        projection_camera_role="camera_front",
        include_inactive=True,
    )

    assert isinstance(result, CameraAnnotationMemberList)
    assert isinstance(result.items[0], CameraAnnotationMember)
    assert result.items[0].relation_status == "stale"
    assert result.items[0].future_field == {"preserved": True}
    assert result.track_revision == 8
    assert dict(route.calls.last.request.url.params) == {
        "scene_track_id": TRACK_ID,
        "projection_camera_role": "camera_front",
        "include_inactive": "true",
    }


def test_calibration_history_encodes_role_and_keeps_virtual_revision(
    client, respx_mock
):
    route = respx_mock.get(
        f"{API}/tasks/{TASK_ID}/point-cloud/cameras/camera_front/calibration"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "current_revision": 1,
                "current_digest": CALIBRATION_DIGEST,
                "items": [
                    _revision(revision=1, digest=CALIBRATION_DIGEST, created_at=None)
                ],
            },
        )
    )

    result = PointCloud(client._http).calibration_history(TASK_ID, "camera_front")

    assert isinstance(result, SensorCalibrationHistory)
    assert result.items[0].created_at is None
    assert result.items[0].calibration.future_calibration_mode == "stereo-v2"
    assert route.called
    with pytest.raises(ValueError, match="camera_"):
        PointCloud(client._http).calibration_history(TASK_ID, "front")


def test_camera_member_create_update_delete_restore_use_exact_routes_and_cas(
    client, respx_mock
):
    create = respx_mock.post(f"{API}/tasks/{TASK_ID}/point-cloud/camera-members").mock(
        return_value=httpx.Response(201, json=_member(version=1, is_active=True))
    )
    update = respx_mock.patch(
        f"{API}/tasks/{TASK_ID}/point-cloud/camera-members/{MEMBER_ID}"
    ).mock(return_value=httpx.Response(200, json=_member(version=3, track_revision=9)))
    delete = respx_mock.delete(
        f"{API}/tasks/{TASK_ID}/point-cloud/camera-members/{MEMBER_ID}"
    ).mock(return_value=httpx.Response(200, json=_member(version=4, is_active=False)))
    restore = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/point-cloud/camera-members/{MEMBER_ID}/restore"
    ).mock(return_value=httpx.Response(200, json=_member(version=5, is_active=True)))
    resource = PointCloud(client._http)

    created = resource.create_camera_member(
        TASK_ID,
        CameraAnnotationMemberCreate(
            source_annotation_id=SOURCE_ANNOTATION_ID,
            camera_role="camera_front",
            bbox=NormalizedCameraBbox(x=0.1, y=0.2, w=0.4, h=0.3),
            expected_track_revision=7,
            expected_calibration_revision=2,
            expected_calibration_digest=CURRENT_CALIBRATION_DIGEST,
        ),
    )
    updated = resource.update_camera_member(
        TASK_ID,
        MEMBER_ID,
        CameraAnnotationMemberUpdate(
            visibility="occluded",
            expected_version=2,
            expected_track_revision=8,
            expected_calibration_revision=2,
            expected_calibration_digest=CURRENT_CALIBRATION_DIGEST,
        ),
    )
    deleted = resource.delete_camera_member(
        TASK_ID,
        MEMBER_ID,
        CameraAnnotationMemberDelete(expected_version=3, expected_track_revision=9),
    )
    restored = resource.restore_camera_member(
        TASK_ID,
        MEMBER_ID,
        CameraAnnotationMemberRestore(
            expected_version=4,
            expected_track_revision=10,
            expected_calibration_revision=2,
            expected_calibration_digest=CURRENT_CALIBRATION_DIGEST,
        ),
    )

    assert created.version == 1
    assert updated.track_revision == 9
    assert deleted.is_active is False
    assert restored.is_active is True
    assert json.loads(create.calls.last.request.content) == {
        "source_annotation_id": SOURCE_ANNOTATION_ID,
        "camera_role": "camera_front",
        "bbox": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3},
        "visibility": "visible",
        "expected_track_revision": 7,
        "expected_calibration_revision": 2,
        "expected_calibration_digest": CURRENT_CALIBRATION_DIGEST,
    }
    assert json.loads(update.calls.last.request.content)["expected_version"] == 2
    assert json.loads(delete.calls.last.request.content) == {
        "expected_version": 3,
        "expected_track_revision": 9,
    }
    assert (
        json.loads(restore.calls.last.request.content)["expected_calibration_revision"]
        == 2
    )


def test_calibration_update_uses_revision_digest_cas_once(client, respx_mock):
    idempotency_key = str(uuid4())
    route = respx_mock.patch(
        f"{API}/tasks/{TASK_ID}/point-cloud/cameras/camera_front/calibration"
    ).mock(return_value=httpx.Response(200, json=_revision()))
    body = SensorCalibrationUpdate(
        calibration=SensorCalibration(
            extrinsic=[0] * 16,
            intrinsic=[1] * 9,
        ),
        expected_revision=1,
        expected_digest=CALIBRATION_DIGEST,
    )

    result = PointCloud(client._http).update_calibration(
        TASK_ID,
        "camera_front",
        body,
        idempotency_key=idempotency_key,
    )

    assert isinstance(result, SensorCalibrationRevision)
    assert result.revision == 2
    assert json.loads(route.calls.last.request.content) == {
        "calibration": {"extrinsic": [0] * 16, "intrinsic": [1] * 9, "rect": None},
        "expected_revision": 1,
        "expected_digest": CALIBRATION_DIGEST,
    }
    assert route.calls.last.request.headers["Idempotency-Key"] == idempotency_key


def test_camera_request_models_are_strict_and_preserve_fixed_matrix_shapes():
    with pytest.raises(ValidationError):
        CameraAnnotationMemberCreate(
            source_annotation_id=SOURCE_ANNOTATION_ID,
            camera_role="camera_front",
            bbox={"x": 0, "y": 0, "w": 1, "h": 1, "future": True},
            expected_track_revision=1,
            expected_calibration_revision=1,
            expected_calibration_digest=CALIBRATION_DIGEST,
        )
    with pytest.raises(ValidationError, match="inside"):
        NormalizedCameraBbox(x=0.8, y=0, w=0.3, h=0.2)
    with pytest.raises(ValidationError):
        SensorCalibration(extrinsic=[0] * 15, intrinsic=[0] * 9)
    with pytest.raises(ValidationError):
        SensorCalibration(extrinsic=[0] * 16, intrinsic=[0] * 9, rect=[0] * 15)
    with pytest.raises(ValidationError):
        CameraAnnotationMemberUpdate(
            expected_version=1,
            expected_track_revision=1,
            expected_calibration_revision=1,
            expected_calibration_digest=CALIBRATION_DIGEST,
        )


def test_member_write_conflict_is_never_retried(client, respx_mock):
    route = respx_mock.patch(
        f"{API}/tasks/{TASK_ID}/point-cloud/camera-members/{MEMBER_ID}"
    ).mock(
        return_value=httpx.Response(
            409,
            json={"detail": {"reason": "track_revision_conflict"}},
        )
    )
    with pytest.raises(ConflictError):
        PointCloud(client._http).update_camera_member(
            TASK_ID,
            MEMBER_ID,
            CameraAnnotationMemberUpdate(
                bbox={"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3},
                expected_version=2,
                expected_track_revision=7,
                expected_calibration_revision=2,
                expected_calibration_digest=CURRENT_CALIBRATION_DIGEST,
            ),
        )
    assert len(route.calls) == 1
