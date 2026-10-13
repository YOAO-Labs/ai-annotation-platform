from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import uuid
import zipfile

import pytest
from pycocotools.coco import COCO
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.schemas._jsonb_types import SensorCalibration
from app.schemas.export import LidarExportOptions
from app.services.exporting.lidar import (
    LidarCameraExportCtx,
    LidarFrameExportCtx,
    MulticameraCocoImageCtx,
    NuScenesSensorExportCtx,
    build_kitti_lidar_frame,
    build_kitti_lidar_label_lines,
    build_multicamera_coco,
    build_nuscenes_frame_records,
    build_pointmask_label_bytes,
)
from app.services.exporting.lidar_preflight import (
    calibration_is_valid,
    preflight_lidar_export,
)
from app.services.exporting.packaging import (
    _FETCH_NUSCENES_MEDIA_TEMPLATE,
    build_export_zip,
    clean_export_targets,
)
from app.services.task_dataset_link import link_items
from tests.factory import create_project


def _ann(
    *,
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    class_name: str = "car",
    geometry: dict | None = None,
    attributes: dict | None = None,
    track_id: str | None = None,
) -> Annotation:
    geometry = geometry or {
        "type": "box_3d",
        "center": [10.0, 0.0, 0.0],
        "size": [4.0, 2.0, 1.5],
        "rotation": [0.0, 0.0, 0.0],
    }
    return Annotation(
        id=uuid.uuid4(),
        task_id=task_id or uuid.uuid4(),
        project_id=project_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        annotation_type=geometry.get("type", "box_3d"),
        tool_unit_id=(
            "point_mask_3d"
            if geometry.get("type") == "point_mask_3d"
            else "lidar_box_3d"
        ),
        class_name=class_name,
        geometry=geometry,
        attributes=attributes or {},
        track_id=track_id,
    )


def _calib() -> dict:
    return {
        "extrinsic": [0, -1, 0, 0, 0, 0, -1, 0, 1, 0, 0, 0, 0, 0, 0, 1],
        "intrinsic": [100, 0, 100, 0, 100, 60, 0, 0, 1],
        "rect": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    }


def _camera() -> LidarCameraExportCtx:
    return LidarCameraExportCtx(
        role="camera_front",
        name="front",
        calibration=SensorCalibration.model_validate(_calib()),
        width=200,
        height=120,
    )


def test_nuscenes_fetch_script_rejects_unsafe_paths_and_verifies_downloads(
    tmp_path,
):
    namespace = {
        "__file__": str(tmp_path / "fetch_nuscenes_media.py"),
        "__name__": "test",
    }
    exec(
        compile(_FETCH_NUSCENES_MEDIA_TEMPLATE, "fetch_nuscenes_media.py", "exec"),
        namespace,
    )
    namespace["HERE"] = str(tmp_path)

    for unsafe_path in (
        "../escape.bin",
        r"..\escape.bin",
        r"C:\escape.bin",
        "//server/share.bin",
        "samples//file.bin",
        "samples/\0file.bin",
    ):
        with pytest.raises(ValueError, match="unsafe media path"):
            namespace["safe_destination"](unsafe_path)

    source = tmp_path / "source.bin"
    source.write_bytes(b"trusted-source")
    manifest_path = tmp_path / "media_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "media": [
                    {
                        "rel_path": "samples/LIDAR_TOP/source.bin",
                        "presigned_url": source.as_uri(),
                        "source_file_size": source.stat().st_size,
                        "source_sha256": hashlib.sha256(
                            source.read_bytes()
                        ).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert namespace["main"]() == 0
    destination = tmp_path / "samples" / "LIDAR_TOP" / "source.bin"
    assert destination.read_bytes() == b"trusted-source"

    destination.write_bytes(b"corrupt")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["media"][0]["source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert namespace["main"]() == 1
    assert destination.read_bytes() == b"corrupt"
    assert list(destination.parent.glob(".aap-download-*")) == []

    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(
            {
                "media": [
                    {
                        "rel_path": "samples/LIDAR_TOP/source.bin",
                        "presigned_url": source.as_uri(),
                        "source_file_size": source.stat().st_size,
                        "source_sha256": fingerprint,
                    },
                    {
                        "rel_path": "samples/LIDAR_TOP/source.bin",
                        "presigned_url": source.as_uri(),
                        "source_file_size": source.stat().st_size,
                        "source_sha256": fingerprint,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert namespace["main"]() == 1

    manifest_path.write_text(
        json.dumps({"media": [{"rel_path": "samples/missing-url.bin"}]}),
        encoding="utf-8",
    )
    assert namespace["main"]() == 1


def test_multicamera_coco_is_deterministic_and_pycocotools_compatible():
    task_front = uuid.UUID("10000000-0000-0000-0000-000000000001")
    task_back = uuid.UUID("10000000-0000-0000-0000-000000000002")
    scene_track_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
    member = _ann(
        task_id=task_front,
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        attributes={"weather": "rain"},
        track_id="trk_car",
    )
    member.id = uuid.UUID("30000000-0000-0000-0000-000000000001")
    member.source = "manual"
    member.version = 4
    member.scene_track_id = scene_track_id
    member.sensor_role = "camera_front"
    member.sensor_dataset_item_id = uuid.UUID("40000000-0000-0000-0000-000000000001")
    member.sensor_visibility = "visible"
    member.calibration_revision = 1
    member.calibration_digest = "a" * 64

    images = [
        MulticameraCocoImageCtx(
            task_id=task_back,
            dataset_item_id=uuid.UUID("40000000-0000-0000-0000-000000000002"),
            sensor_role="camera_back",
            file_name=f"images/camera_back/{task_back}/frame.jpg",
            width=320,
            height=200,
        ),
        MulticameraCocoImageCtx(
            task_id=task_front,
            dataset_item_id=member.sensor_dataset_item_id,
            sensor_role="camera_front",
            file_name=f"images/camera_front/{task_front}/frame.jpg",
            width=200,
            height=100,
            members=[member],
            scene_id=uuid.UUID("50000000-0000-0000-0000-000000000001"),
            frame_index=7,
            current_calibration_revision=2,
            current_calibration_digest="b" * 64,
        ),
    ]

    first = build_multicamera_coco(
        images,
        classes=["car", "pedestrian"],
        include_attributes=True,
        allowed_attribute_keys={"weather"},
    )
    second = build_multicamera_coco(
        list(reversed(images)),
        classes=["car", "pedestrian"],
        include_attributes=True,
        allowed_attribute_keys={"weather"},
    )

    assert first.document == second.document
    assert first.image_count == 2
    assert first.annotation_count == 1
    assert first.stale_relation_count == 1
    assert first.document["annotations"][0]["bbox"] == pytest.approx([20, 20, 60, 40])
    assert first.document["annotations"][0]["relation_status"] == "stale"
    assert all(row["id"] <= (1 << 53) - 1 for row in first.document["images"])
    coco = COCO()
    coco.dataset = first.document
    coco.createIndex()
    assert set(coco.imgs) == {row["id"] for row in first.document["images"]}
    negative = next(
        row for row in first.document["images"] if row["sensor_role"] == "camera_back"
    )
    assert coco.getAnnIds(imgIds=[negative["id"]]) == []

    member.sensor_role = "camera_back"
    with pytest.raises(ValueError, match="multicamera_coco_member_context_invalid"):
        build_multicamera_coco(
            images,
            classes=["car", "pedestrian"],
            include_attributes=True,
            allowed_attribute_keys={"weather"},
        )


def test_kitti_lidar_label_projects_bbox_and_camera_bottom_center():
    lines = build_kitti_lidar_label_lines(
        [
            _ann(
                attributes={"occluded": 2, "truncated": 0.25},
            )
        ],
        camera=_camera(),
        axis_convention="iso_8855",
    )

    fields = lines[0].split()
    assert len(fields) == 15
    assert fields[0] == "car"
    assert fields[2] == "2"
    assert [float(value) for value in fields[4:8]] == pytest.approx(
        [87.5, 50.62, 112.5, 69.38]
    )
    assert [float(value) for value in fields[8:11]] == pytest.approx([1.5, 2.0, 4.0])
    assert [float(value) for value in fields[11:14]] == pytest.approx([0.0, 0.75, 10.0])
    assert float(fields[14]) == pytest.approx(-math.pi / 2, abs=1e-6)
    assert float(fields[3]) == pytest.approx(-math.pi / 2, abs=1e-6)


def test_kitti_lidar_prefers_persistent_manual_camera_bbox() -> None:
    scene_track_id = uuid.uuid4()
    source = _ann()
    source.scene_track_id = scene_track_id
    member = _ann(geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4})
    member.scene_track_id = scene_track_id
    member.sensor_role = "camera_front"
    member.sensor_dataset_item_id = uuid.uuid4()
    member.sensor_visibility = "occluded"
    member.calibration_revision = 1
    member.calibration_digest = "a" * 64
    member.is_active = True
    member.was_cancelled = False

    result = build_kitti_lidar_frame(
        [source],
        camera=_camera(),
        axis_convention="iso_8855",
        camera_members=[member],
    )

    fields = result.lines[0].split()
    assert [float(value) for value in fields[4:8]] == pytest.approx([20, 24, 80, 72])
    assert fields[2] == "1"
    assert result.manual_bbox_count == 1
    assert result.derived_bbox_count == 0


@pytest.mark.parametrize(
    ("center", "expected_reason"),
    [
        ([-5.0, 0.0, 0.0], "behind_camera"),
        ([10.0, 50.0, 0.0], "outside_image_or_degenerate"),
    ],
)
def test_kitti_lidar_skips_fully_invisible_boxes(center, expected_reason):
    result = build_kitti_lidar_frame(
        [
            _ann(
                geometry={
                    "type": "box_3d",
                    "center": center,
                    "size": [2, 2, 2],
                    "rotation": [0, 0, 0],
                }
            )
        ],
        camera=_camera(),
        axis_convention="iso_8855",
    )

    assert result.lines == []
    assert result.skipped[0].reason == expected_reason


def test_kitti_lidar_clips_near_plane_and_image_boundary():
    near = build_kitti_lidar_frame(
        [
            _ann(
                geometry={
                    "type": "box_3d",
                    "center": [1, 0, 0],
                    "size": [2, 1, 1],
                    "rotation": [0, 0, 0],
                }
            )
        ],
        camera=_camera(),
        axis_convention="iso_8855",
    )
    partial = build_kitti_lidar_frame(
        [
            _ann(
                geometry={
                    "type": "box_3d",
                    "center": [10, 9.5, 0],
                    "size": [4, 4, 2],
                    "rotation": [0, 0, 0],
                }
            )
        ],
        camera=_camera(),
        axis_convention="iso_8855",
    )

    assert len(near.lines) == 1
    assert len(partial.lines) == 1
    partial_fields = partial.lines[0].split()
    assert float(partial_fields[1]) > 0
    assert float(partial_fields[4]) == 0


def test_kitti_lidar_non_iso_source_axis_matches_iso_projection():
    apollo_camera = LidarCameraExportCtx(
        role="camera_front",
        name="front",
        calibration=SensorCalibration.model_validate(
            {
                **_calib(),
                "extrinsic": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1],
            }
        ),
        width=200,
        height=120,
    )
    annotation = _ann()

    iso = build_kitti_lidar_label_lines(
        [annotation], camera=_camera(), axis_convention="iso_8855"
    )
    apollo = build_kitti_lidar_label_lines(
        [annotation], camera=apollo_camera, axis_convention="apollo"
    )

    assert [float(value) for value in iso[0].split()[1:]] == pytest.approx(
        [float(value) for value in apollo[0].split()[1:]]
    )


def test_pointmask_builds_uint32_labels_with_one_based_category_ids():
    label_bytes = build_pointmask_label_bytes(
        [
            _ann(
                class_name="car",
                geometry={
                    "type": "point_mask_3d",
                    "point_indices": [1, 3],
                    "source_point_count": 5,
                },
            )
        ],
        source_point_count=None,
        category_map={"car": 1},
    )

    assert struct.unpack("<5I", label_bytes) == (0, 1, 0, 1, 0)


def test_nuscenes_serializer_refuses_placeholder_output():
    with pytest.raises(ValueError, match="nuscenes_export_empty"):
        build_nuscenes_frame_records([])


def test_nuscenes_serializer_builds_complete_deterministic_scene_graph():
    scene_id = uuid.uuid4()
    annotations = [_ann(track_id="vehicle-1"), _ann(track_id="vehicle-1")]
    source_scene = {
        "nuscenes_export": {
            "scene": {
                "token": "source-scene",
                "name": "scene-0001",
                "description": "test",
                "log_token": "source-log",
                "nbr_samples": 2,
                "first_sample_token": "source-sample-0",
                "last_sample_token": "source-sample-1",
            },
            "log": {
                "token": "source-log",
                "logfile": "logfile",
                "vehicle": "vehicle",
                "date_captured": "2026-08-27",
                "location": "test-track",
            },
            "map": {
                "token": "source-map",
                "log_tokens": ["source-log"],
                "category": "semantic_prior",
                "filename": "maps/source-map.png",
            },
        }
    }
    frames: list[LidarFrameExportCtx] = []
    for index, annotation in enumerate(annotations):
        source_sample = {
            "token": f"source-sample-{index}",
            "timestamp": 1_000_000 + index * 100_000,
            "scene_token": "source-scene",
            "prev": "source-sample-0" if index else "",
            "next": "source-sample-1" if index == 0 else "",
        }
        sensor = {
            "token": "source-sensor",
            "channel": "LIDAR_TOP",
            "modality": "lidar",
        }
        calibrated = {
            "token": "source-calibrated",
            "sensor_token": "source-sensor",
            "translation": [0, 0, 0],
            "rotation": [1, 0, 0, 0],
            "camera_intrinsic": [],
        }
        ego_pose = {
            "token": f"source-ego-{index}",
            "translation": [float(index), 0, 0],
            "rotation": [1, 0, 0, 0],
            "timestamp": 1_000_010 + index * 100_000,
        }
        sample_data = {
            "token": f"source-sd-{index}",
            "sample_token": source_sample["token"],
            "ego_pose_token": ego_pose["token"],
            "calibrated_sensor_token": calibrated["token"],
            "filename": f"samples/LIDAR_TOP/{index}.pcd.bin",
            "fileformat": "bin",
            "width": 0,
            "height": 0,
            "timestamp": ego_pose["timestamp"],
            "is_key_frame": True,
        }
        frames.append(
            LidarFrameExportCtx(
                task_id=annotation.task_id,
                frame_key=str(index),
                annotations=[annotation],
                axis_convention="iso_8855",
                scene_id=scene_id,
                scene_name="scene-0001",
                scene_source_metadata=source_scene,
                source_sample=source_sample,
                frame_index=index,
                timestamp_us=ego_pose["timestamp"],
                ego_translation=ego_pose["translation"],
                ego_rotation=ego_pose["rotation"],
                sensors={
                    "primary_lidar": NuScenesSensorExportCtx(
                        role="primary_lidar",
                        dataset_item_id=uuid.uuid4(),
                        sample_data=sample_data,
                        calibrated_sensor=calibrated,
                        sensor=sensor,
                        ego_pose=ego_pose,
                        source_storage_key=f"source/{index}.pcd.bin",
                    )
                },
                point_counts={annotation.id: index + 1},
            )
        )

    first = build_nuscenes_frame_records(list(reversed(frames)))
    second = build_nuscenes_frame_records(frames)

    assert first == second
    assert set(first) == {
        "attribute",
        "calibrated_sensor",
        "category",
        "ego_pose",
        "instance",
        "log",
        "map",
        "sample",
        "sample_annotation",
        "sample_data",
        "scene",
        "sensor",
        "visibility",
    }
    assert [row["timestamp"] for row in first["sample"]] == [1_000_000, 1_100_000]
    assert first["scene"][0]["nbr_samples"] == 2
    assert first["instance"][0]["nbr_annotations"] == 2
    exported_annotations = sorted(
        first["sample_annotation"], key=lambda row: row["num_lidar_pts"]
    )
    assert exported_annotations[0]["size"] == [2.0, 4.0, 1.5]
    assert exported_annotations[1]["translation"] == [11.0, 0.0, 0.0]


def test_lidar_preflight_rejects_non_rigid_or_negative_focal_calibration():
    assert calibration_is_valid(_calib()) is True
    sheared = _calib()
    sheared["extrinsic"] = [1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    assert calibration_is_valid(sheared) is False
    negative_focal = _calib()
    negative_focal["intrinsic"] = [-100, 0, 100, 0, 100, 60, 0, 0, 1]
    assert calibration_is_valid(negative_focal) is False


@pytest.mark.asyncio
async def test_lidar_preflight_reports_axis_size_and_calibration_issues(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project = await create_project(
        db_session,
        owner_id=user.id,
        type_key="lidar",
        type_label="点云检测",
        classes=["car"],
    )
    project.data_type = "lidar"
    dataset = Dataset(
        display_id=f"DS-PF-{uuid.uuid4().hex[:6]}",
        name="preflight",
        data_type="point_cloud",
        created_by=user.id,
        metadata_={},
    )
    db_session.add(dataset)
    await db_session.flush()
    lidar_item = DatasetItem(
        dataset_id=dataset.id,
        file_name="000001.pcd",
        file_path="preflight/lidar/000001.pcd",
        file_type="point_cloud",
    )
    camera_item = DatasetItem(
        dataset_id=dataset.id,
        file_name="000001.jpg",
        file_path="preflight/camera/front/000001.jpg",
        file_type="image",
        metadata_={},
    )
    db_session.add_all([lidar_item, camera_item])
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=lidar_item.id,
        display_id=f"T-PF-{uuid.uuid4().hex[:6]}",
        file_name="000001.pcd",
        file_path=lidar_item.file_path,
        file_type="point_cloud",
    )
    db_session.add(task)
    await db_session.flush()
    await link_items(
        db_session,
        task.id,
        [
            (lidar_item.id, "primary_lidar", None),
            (camera_item.id, "camera_front", "front"),
        ],
    )

    report = await preflight_lidar_export(
        db_session,
        project_id=project.id,
        batch_id=None,
        targets=["kitti"],
        options=LidarExportOptions(kitti_camera_role="camera_front"),
    )

    assert report.ready is False
    assert report.camera_roles == ["camera_front"]
    assert {issue.code for issue in report.issues} == {
        "axis_convention_missing",
        "camera_image_size_missing",
        "camera_calibration_invalid",
    }


@pytest.mark.asyncio
async def test_failed_lidar_preflight_creates_no_async_job(
    db_session,
    super_admin,
    httpx_client,
    monkeypatch,
):
    user, token = super_admin
    project = await create_project(
        db_session,
        owner_id=user.id,
        type_key="lidar",
        type_label="点云检测",
        classes=["car"],
    )
    project.data_type = "lidar"
    dispatched = False

    def _dispatch(**_kwargs):
        nonlocal dispatched
        dispatched = True

    monkeypatch.setattr("app.workers.export.run_export.delay", _dispatch)

    preflight_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/exports/lidar:preflight",
        json={"targets": ["kitti"], "lidar": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export?targets=kitti",
        json={"lidar": {"kitti_camera_role": "camera_front"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    coco_preflight_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/exports/lidar:preflight",
        json={"targets": ["coco-multicamera"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    coco_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export?targets=coco-multicamera",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert preflight_response.status_code == 200
    assert preflight_response.json()["issues"][0]["code"] == "kitti_camera_required"
    assert response.status_code == 409
    assert response.json()["detail"]["ready"] is False
    assert coco_preflight_response.status_code == 200
    assert coco_preflight_response.json()["issues"][0]["code"] == (
        "multicamera_coco_export_empty"
    )
    assert coco_response.status_code == 409
    assert coco_response.json()["detail"]["issues"][0]["code"] == (
        "multicamera_coco_export_empty"
    )
    assert dispatched is False
    job_count = await db_session.scalar(
        select(func.count(AsyncJob.id)).where(AsyncJob.project_id == project.id)
    )
    assert job_count == 0


@pytest.mark.asyncio
async def test_lidar_export_zip_writes_standard_targets(
    db_session,
    super_admin,
    httpx_client,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda key, **_kwargs: f"signed://{key}",
    )
    user, token = super_admin
    project = await create_project(
        db_session,
        owner_id=user.id,
        type_key="lidar",
        type_label="点云检测",
        classes=["car"],
    )
    project.data_type = "lidar"
    project.tool_bindings = {
        "lidar_box_3d": {
            "enabled": True,
            "classes": [{"name": "car", "order": 0}],
            "attribute_schema": {
                "fields": [
                    {"key": "occluded"},
                    {"key": "truncated"},
                    {"key": "visible"},
                ]
            },
        },
        "point_mask_3d": {
            "enabled": True,
            "classes": [{"name": "car", "order": 0}],
            "attribute_schema": {"fields": []},
        },
    }
    ds = Dataset(
        display_id=f"DS-LX-{uuid.uuid4().hex[:6]}",
        name="scene-a",
        data_type="point_cloud",
        created_by=user.id,
        metadata_={"axis_convention": "iso_8855"},
    )
    db_session.add(ds)
    await db_session.flush()
    lidar_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.pcd",
        file_path="scene-a/lidar/000001.pcd",
        file_type="point_cloud",
        file_size=48,
        metadata_={"point_count": 4},
    )
    cam_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene-a/camera/front/000001.jpg",
        file_type="image",
        width=200,
        height=120,
        metadata_={"calibration": _calib()},
    )
    cam_back_item = DatasetItem(
        dataset_id=ds.id,
        file_name="000001.jpg",
        file_path="scene-a/camera/back/000001.jpg",
        file_type="image",
        width=200,
        height=120,
        metadata_={"calibration": _calib()},
    )
    db_session.add_all([lidar_item, cam_item, cam_back_item])
    await db_session.flush()
    batch = TaskBatch(
        project_id=project.id,
        dataset_id=ds.id,
        display_id=f"B-LX-{uuid.uuid4().hex[:6]}",
        name="LiDAR export batch",
        created_by=user.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=lidar_item.id,
        batch_id=batch.id,
        display_id=f"T-LX-{uuid.uuid4().hex[:6]}",
        file_name="000001.pcd",
        file_path=lidar_item.file_path,
        file_type="point_cloud",
    )
    db_session.add(task)
    await db_session.flush()
    await link_items(
        db_session,
        task.id,
        [
            (lidar_item.id, "primary_lidar", None),
            (cam_item.id, "camera_front", "front"),
            (cam_back_item.id, "camera_back", "back"),
        ],
    )
    db_session.add(
        _ann(
            task_id=task.id,
            project_id=project.id,
            user_id=user.id,
            attributes={"occluded": 1, "truncated": 0.5, "visible": "v80-100"},
        )
    )
    db_session.add(
        _ann(
            task_id=task.id,
            project_id=project.id,
            user_id=user.id,
            class_name="car",
            geometry={
                "type": "point_mask_3d",
                "point_indices": [0, 2],
                "source_point_count": 4,
            },
        )
    )
    await db_session.flush()

    with monkeypatch.context() as scope_patch:
        scope_patch.setattr(
            "app.services.exporting.lidar_preflight.MAX_NUSCENES_EXPORT_FRAMES",
            0,
        )
        oversized_frames = await preflight_lidar_export(
            db_session,
            project_id=project.id,
            batch_id=None,
            targets=["nuscenes"],
            options=None,
        )
    assert oversized_frames.checked_tasks == 1
    assert [issue.code for issue in oversized_frames.issues] == [
        "nuscenes_export_too_large"
    ]

    with monkeypatch.context() as scope_patch:
        scope_patch.setattr(
            "app.services.exporting.lidar_preflight.MAX_NUSCENES_EXPORT_BOXES",
            0,
        )
        oversized_boxes = await preflight_lidar_export(
            db_session,
            project_id=project.id,
            batch_id=None,
            targets=["nuscenes"],
            options=None,
        )
    assert oversized_boxes.checked_tasks == 1
    assert [issue.code for issue in oversized_boxes.issues] == [
        "nuscenes_export_too_large"
    ]

    with monkeypatch.context() as scope_patch:
        scope_patch.setattr(
            "app.services.exporting.lidar_preflight.MAX_NUSCENES_POINT_BOX_TESTS",
            0,
        )
        oversized_point_work = await preflight_lidar_export(
            db_session,
            project_id=project.id,
            batch_id=None,
            targets=["nuscenes"],
            options=None,
        )
    assert "nuscenes_export_too_large" in {
        issue.code for issue in oversized_point_work.issues
    }

    with monkeypatch.context() as scope_patch:
        scope_patch.setattr(
            "app.services.exporting.lidar_preflight.MAX_NUSCENES_PCD_BYTES_TOTAL",
            0,
        )
        oversized_pcd_bytes = await preflight_lidar_export(
            db_session,
            project_id=project.id,
            batch_id=None,
            targets=["nuscenes"],
            options=None,
        )
    assert "nuscenes_export_too_large" in {
        issue.code for issue in oversized_pcd_bytes.issues
    }

    zip_path, file_count, _size = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        targets=["kitti", "pointmask"],
        include_attributes=True,
        video_frame_mode="keyframes",
        format_options={"lidar": {"kitti_camera_role": "camera_front"}},
    )
    try:
        assert file_count >= 2
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert "kitti/label_2/lidar/000001.txt" in names
            assert "kitti/calib/lidar/000001.txt" in names
            assert "kitti/calib_raw/front/lidar/000001.json" in names
            assert "pointmask/segmentation/lidar/000001.label" in names
            assert "pointmask/category_map.json" in names
            assert "pointmask/fetch_pointclouds.py" in names
            manifest = json.loads(zf.read("kitti/images_manifest.json"))
            assert len(manifest["images"]) == 1
            assert manifest["images"][0]["camera"] == "front"
            pointmask_manifest = json.loads(zf.read("pointmask/images_manifest.json"))
            assert {item["camera"] for item in pointmask_manifest["images"]} == {
                "front",
                "back",
            }
            assert "pointmask/calib_raw/back/lidar/000001.json" in names
            label = zf.read("kitti/label_2/lidar/000001.txt").decode()
            assert label.split()[2] == "1"
            assert "-1.00 -1.00 -1.00 -1.00" not in label
            report = json.loads(zf.read("kitti/export_report.json"))
            assert report["camera_role"] == "camera_front"
            assert report["exported_annotations"] == 1
            pointmask = zf.read("pointmask/segmentation/lidar/000001.label")
            assert struct.unpack("<4I", pointmask) == (1, 0, 1, 0)
    finally:
        os.unlink(zip_path)

    with pytest.raises(ValueError, match="nuscenes_scene_frame_missing"):
        await build_export_zip(
            db_session,
            project.id,
            batch_id=None,
            targets=["nuscenes"],
            include_attributes=True,
            video_frame_mode="keyframes",
        )

    dispatched: dict = {}
    monkeypatch.setattr(
        "app.workers.export.run_export.delay",
        lambda **kwargs: dispatched.update(kwargs),
    )
    preflight_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/exports/lidar:preflight",
        json={
            "targets": ["kitti", "pointmask"],
            "lidar": {"kitti_camera_role": "camera_front"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    batch_preflight_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/{batch.id}/exports/lidar:preflight",
        json={
            "targets": ["kitti"],
            "lidar": {"kitti_camera_role": "camera_front"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    export_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export?targets=kitti&targets=pointmask",
        json={"lidar": {"kitti_camera_role": "camera_front"}},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert preflight_response.status_code == 200
    assert preflight_response.json()["ready"] is True
    assert batch_preflight_response.status_code == 200
    assert batch_preflight_response.json()["checked_tasks"] == 1
    assert batch_preflight_response.json()["ready"] is True
    assert export_response.status_code == 202
    assert dispatched["opts"]["lidar"] == {"kitti_camera_role": "camera_front"}


def test_clean_export_targets_filters_lidar_targets():
    assert clean_export_targets(
        ["coco-multicamera", "kitti", "pointmask", "kitti"], data_type="lidar"
    ) == ["coco-multicamera", "kitti", "pointmask"]
    with pytest.raises(ValueError, match="lidar project"):
        clean_export_targets(["coco"], data_type="lidar")
