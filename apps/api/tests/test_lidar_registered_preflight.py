"""Registered LiDAR preflight keeps business validation and never reads media."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
import sys
import uuid

import pytest

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.scene_pose import SceneFramePose
from app.db.models.scene_track import SceneTrack
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.schemas.export import LidarExportOptions
from app.services.content_snapshot import ContentError
from app.services.exporting import lidar_preflight as preflight
from app.services.sensor_calibration import calibration_digest
from app.schemas._jsonb_types import SensorCalibration


def calibration():
    return {
        "extrinsic": [0, -1, 0, 0, 0, 0, -1, 0, 1, 0, 0, 0, 0, 0, 0, 1],
        "intrinsic": [100, 0, 100, 0, 100, 60, 0, 0, 1],
    }


def forbidden(*args, **kwargs):
    pytest.fail("registered preflight must not use live rows, media I/O or commits")


@pytest.fixture
def world(monkeypatch):
    project = Project(
        id=uuid.uuid4(),
        display_id="P-PREFLIGHT",
        data_type="lidar",
        tool_bindings={
            "lidar_box_3d": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    dataset = Dataset(id=uuid.uuid4(), metadata_={"axis_convention": "iso_8855"})
    primary = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="0001.pcd",
        file_path="saved/private/0001.pcd",
        file_type="point_cloud",
        file_size=999_999_999,
        content_hash="legacy-md5",
        metadata_={"point_count": 4},
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=None,
        display_id="T-PREFLIGHT",
        file_name=primary.file_name,
        file_path=primary.file_path,
        file_type="point_cloud",
    )
    items = {primary.id: primary}
    roles = {}
    primary_link = TaskDatasetItemLink(
        id=uuid.uuid4(),
        task_id=task.id,
        dataset_item_id=primary.id,
        role="primary_lidar",
    )
    roles[primary_link.role] = (primary_link, primary)
    media = [
        {
            "task_id": str(task.id),
            "role": "primary_lidar",
            "source": {
                "item_id": str(primary.id),
                "object_key": primary.file_path,
                "size_bytes": 48,
                "sha256": "a" * 64,
            },
        }
    ]
    cameras = {}
    for role in ("camera_front", "camera_back"):
        camera = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name=role + ".jpg",
            file_path="saved/private/" + role + ".jpg",
            file_type="image",
            file_size=999_999_999,
            content_hash="old-md5",
            width=200,
            height=120,
            metadata_={"calibration": calibration()},
        )
        link = TaskDatasetItemLink(
            id=uuid.uuid4(),
            task_id=task.id,
            dataset_item_id=camera.id,
            role=role,
        )
        roles[role] = (link, camera)
        cameras[role] = camera
        items[camera.id] = camera
        media.append(
            {
                "task_id": str(task.id),
                "role": role,
                "source": {
                    "item_id": str(camera.id),
                    "object_key": camera.file_path,
                    "size_bytes": 37,
                    "sha256": "b" * 64,
                },
            }
        )
    view = SimpleNamespace(
        project=project,
        tasks=[task],
        items=items,
        datasets={dataset.id: dataset},
        links_by_task={task.id: roles},
        annotations=[],
        tracks=[],
        calibrations=[],
        scenes_by_id={},
        poses_by_frame={},
        all_scene_tasks={},
    )
    snapshot = {"media": media, "bytes": {"image": 74, "point_cloud": 48}}
    calls = []

    async def get(model, key):
        assert model is Project and key == project.id
        calls.append((model, key))
        return project

    db = SimpleNamespace(
        get=get,
        execute=forbidden,
        scalar=forbidden,
        scalars=forbidden,
        commit=forbidden,
        flush=forbidden,
        add=forbidden,
    )

    async def collect(db_arg, project_id, batch_id, targets, options):
        assert db_arg is db and project_id == project.id
        calls.append((batch_id, targets, options))
        return preflight._RegisteredPreflight(snapshot, view)

    monkeypatch.setattr(preflight, "_collect_registered", collect)
    monkeypatch.setattr(preflight, "resolve_calibration_states", forbidden)
    for name in ("verify_upload", "generate_download_url", "download_file"):
        monkeypatch.setattr(preflight.storage_service, name, forbidden, raising=False)
    for name in ("head_object", "get_object", "download_file"):
        monkeypatch.setattr(preflight.storage_service.client, name, forbidden)
    return SimpleNamespace(
        project=project,
        dataset=dataset,
        primary=primary,
        task=task,
        cameras=cameras,
        view=view,
        snapshot=snapshot,
        calls=calls,
        db=db,
    )


async def report(world, targets=("kitti",), *, options=True):
    return await preflight.preflight_lidar_export(
        world.db,
        project_id=world.project.id,
        batch_id=None,
        targets=list(targets),
        options=LidarExportOptions(kitti_camera_role="camera_front")
        if options
        else None,
    )


def codes(result):
    return {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    "targets",
    [
        ("kitti",),
        ("pointmask",),
        ("coco-multicamera",),
        ("kitti", "pointmask"),
        ("kitti", "coco-multicamera"),
        ("pointmask", "coco-multicamera"),
        ("kitti", "pointmask", "coco-multicamera"),
    ],
)
async def test_registered_formats_use_only_frozen_facts(world, targets):
    result = await report(world, targets)
    assert result.ready, result.model_dump()
    assert result.checked_tasks == 1
    assert len(world.calls) == 2
    assert world.task.dataset_item_id is None
    assert world.primary.content_hash == "legacy-md5"
    assert world.primary.file_size == 999_999_999
    assert all(item.file_size == 999_999_999 for item in world.cameras.values())


async def test_camera_only_coco_needs_no_primary_or_axis(world):
    world.view.items.pop(world.primary.id)
    world.view.links_by_task[world.task.id].pop("primary_lidar")
    world.snapshot["media"] = world.snapshot["media"][1:]
    world.dataset.metadata_ = {}
    assert (await report(world, ("coco-multicamera",))).ready


async def test_pointmask_task_original_needs_no_item_or_axis(world):
    world.snapshot["media"][0]["source"]["item_id"] = None
    world.view.items.pop(world.primary.id)
    world.view.links_by_task[world.task.id].pop("primary_lidar")
    world.dataset.metadata_ = {}
    result = await report(world, ("pointmask",))
    assert result.ready and result.checked_tasks == 1


@pytest.mark.parametrize("axis", [None, "raw", "invalid"])
async def test_kitti_keeps_trusted_axis_requirement(world, axis):
    world.dataset.metadata_ = {"axis_convention": axis}
    result = await report(world)
    assert not result.ready
    assert codes(result) == {
        "axis_convention_missing" if axis is None else "axis_convention_untrusted"
    }


async def test_kitti_task_original_without_axis_is_rejected(world):
    world.snapshot["media"][0]["source"]["item_id"] = None
    result = await report(world)
    assert {"primary_lidar_missing", "axis_convention_missing"} <= codes(result)


async def test_kitti_preserves_explicit_camera_and_calibration_checks(world):
    result = await report(world, options=False)
    assert "kitti_camera_required" in codes(result)
    world.cameras["camera_front"].metadata_ = {}
    world.cameras["camera_front"].width = None
    result = await report(world)
    assert {"camera_calibration_invalid", "camera_image_size_missing"} <= codes(result)


@pytest.mark.parametrize(
    "code",
    [
        "content_unverified",
        "original_relation_conflict",
        "no_readable_original",
        "location_unavailable",
        "lidar_export_input_budget_exceeded",
        "lidar_export_pointmask_invalid",
        "auxiliary_original_unpinned",
        "auxiliary_original_contract_changed",
        "kitti_camera_required",
        "multicamera_coco_camera_missing",
    ],
)
async def test_collector_failures_become_readable_stable_issues(
    world, monkeypatch, code
):
    async def reject(*args, **kwargs):
        raise ContentError(code)

    monkeypatch.setattr(preflight, "_collect_registered", reject)
    result = await report(world)
    assert not result.ready and codes(result) == {code}
    assert result.issue_count == 1 and not result.issues_truncated
    assert any("\u4e00" <= char <= "\u9fff" for char in result.issues[0].message)


async def test_unknown_collector_error_does_not_leak_provider_text(world, monkeypatch):
    async def reject(*args, **kwargs):
        raise ContentError("SDK denied https://secret.invalid/signed?signature=private")

    monkeypatch.setattr(preflight, "_collect_registered", reject)
    result = await report(world, ("pointmask",))
    assert codes(result) == {"lidar_export_original_unavailable"}
    assert "secret" not in result.model_dump_json()
    assert "signature" not in result.model_dump_json()


async def test_collect_adapter_passes_scope_and_options_without_write(
    world, monkeypatch
):
    collected = []

    async def collect(db, project_id, batch_id, targets, options):
        collected.append((db, project_id, batch_id, targets, options))
        return SimpleNamespace(snapshot=world.snapshot)

    module = ModuleType("app.services.exporting.lidar_inputs")
    module.collect = collect
    frozen_module = ModuleType("app.services.exporting.lidar_frozen")
    frozen_module.FrozenLidar = lambda snapshot: world.view
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, frozen_module.__name__, frozen_module)
    batch_id = uuid.uuid4()
    value = await _REAL_COLLECT(
        world.db,
        world.project.id,
        batch_id,
        ["pointmask", "coco-multicamera"],
        LidarExportOptions(kitti_camera_role="camera_front"),
    )
    assert value.view is world.view
    assert collected == [
        (
            world.db,
            world.project.id,
            batch_id,
            ["pointmask", "coco-multicamera"],
            {"lidar": {"kitti_camera_role": "camera_front"}},
        )
    ]
    item = value.item(world.task.id, "primary_lidar")
    assert item.file_size == 48 and item.content_hash == "a" * 64
    with pytest.raises(FrozenInstanceError):
        item.file_size = 49


_REAL_COLLECT = preflight._collect_registered


def manual_members(world):
    track = SceneTrack(
        id=uuid.uuid4(),
        project_id=world.project.id,
        class_name="car",
        track_id="car-1",
        retired_at=None,
    )
    parent = Annotation(
        id=uuid.uuid4(),
        project_id=world.project.id,
        task_id=world.task.id,
        class_name="car",
        track_id=track.track_id,
        scene_track_id=track.id,
        geometry={
            "type": "box_3d",
            "center": [0, 0, 0],
            "size": [2, 3, 4],
            "rotation": [0, 0, 0],
        },
        is_active=True,
        was_cancelled=False,
    )
    member = Annotation(
        id=uuid.uuid4(),
        project_id=world.project.id,
        task_id=world.task.id,
        class_name="car",
        source="manual",
        sensor_role="camera_front",
        sensor_dataset_item_id=world.cameras["camera_front"].id,
        sensor_visibility="visible",
        track_id=track.track_id,
        scene_track_id=track.id,
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3},
        calibration_revision=1,
        calibration_digest="c" * 64,
        is_active=True,
        was_cancelled=False,
    )
    world.view.annotations[:] = [parent, member]
    world.view.tracks[:] = [track]
    return parent, member, track


async def test_coco_allows_negative_images_without_calibration(world):
    for camera in world.cameras.values():
        camera.metadata_ = {}
    assert (await report(world, ("coco-multicamera",))).ready


async def test_coco_stale_manual_context_stays_compatible(world):
    _parent, member, _track = manual_members(world)
    world.view.calibrations = [
        SimpleNamespace(
            dataset_item_id=member.sensor_dataset_item_id,
            revision=2,
            digest="d" * 64,
            calibration=calibration(),
            created_at=datetime.now(timezone.utc),
        )
    ]
    assert (await report(world, ("coco-multicamera",))).ready


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("class_name", "unknown", "multicamera_coco_category_unknown"),
        ("geometry", {"type": "bbox", "x": -1}, "multicamera_coco_bbox_invalid"),
        ("sensor_dataset_item_id", None, "multicamera_coco_member_context_invalid"),
        ("calibration_revision", None, "multicamera_coco_member_context_invalid"),
        ("calibration_digest", "old", "multicamera_coco_member_context_invalid"),
        ("project_id", None, "multicamera_coco_member_context_invalid"),
        ("scene_track_id", None, "multicamera_coco_member_context_invalid"),
    ],
)
async def test_coco_manual_member_validation_is_preserved(
    world, field, value, expected
):
    _parent, member, _track = manual_members(world)
    setattr(member, field, value)
    assert expected in codes(await report(world, ("coco-multicamera",)))


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "inactive", "retired", "class"]
)
async def test_coco_requires_unique_active_matching_3d_parent(world, fault):
    parent, _member, track = manual_members(world)
    if fault == "missing":
        world.view.annotations.remove(parent)
    elif fault == "duplicate":
        world.view.annotations.append(parent)
    elif fault == "inactive":
        parent.is_active = False
    elif fault == "retired":
        track.retired_at = datetime.now(timezone.utc)
    else:
        track.class_name = "wrong"
    assert "multicamera_coco_parent_invalid" in codes(
        await report(world, ("coco-multicamera",))
    )


async def test_coco_manual_camera_still_requires_valid_current_calibration(world):
    manual_members(world)
    world.cameras["camera_front"].metadata_ = {}
    assert "multicamera_coco_calibration_invalid" in codes(
        await report(world, ("coco-multicamera",))
    )


@pytest.mark.parametrize("fault", ["dimensions", "path", "type", "duplicate"])
async def test_coco_camera_metadata_validation_is_preserved(world, fault):
    camera = world.cameras["camera_front"]
    if fault == "dimensions":
        camera.width = 0
    elif fault == "path":
        camera.file_path = "bad/..\\escape.jpg"
    elif fault == "type":
        camera.file_type = "point_cloud"
    else:
        link, _ = world.view.links_by_task[world.task.id]["camera_back"]
        world.view.links_by_task[world.task.id]["camera_back"] = (link, camera)
        world.snapshot["media"][2]["source"]["item_id"] = str(camera.id)
    result = await report(world, ("coco-multicamera",))
    assert (
        "multicamera_coco_camera_link_duplicate"
        if fault == "duplicate"
        else "multicamera_coco_camera_item_invalid"
    ) in codes(result)


async def test_coco_budgets_canonical_distinct_media_bytes(world, monkeypatch):
    monkeypatch.setattr(preflight, "MAX_MULTICAMERA_COCO_MEDIA_BYTES", 74)
    assert (await report(world, ("coco-multicamera",))).ready
    world.snapshot["bytes"]["image"] = 75
    assert "multicamera_coco_export_too_large" in codes(
        await report(world, ("coco-multicamera",))
    )


async def test_issue_limit_retains_count_and_truncation(world, monkeypatch):
    monkeypatch.setattr(preflight, "MAX_PREFLIGHT_ISSUES", 1)
    world.cameras["camera_front"].width = 0
    world.cameras["camera_back"].height = 0
    result = await report(world, ("coco-multicamera",))
    assert result.issue_count == 2
    assert len(result.issues) == 1 and result.issues_truncated


def nuscenes(world):
    scene_id = uuid.uuid4()
    sample = {
        "token": "sample-0",
        "scene_token": "scene",
        "timestamp": 100,
        "prev": "",
        "next": "",
    }
    scene = Scene(
        id=scene_id,
        source_format="nuscenes",
        source_metadata={
            "frame": "ego",
            "nuscenes_export": {
                "scene": {
                    "token": "scene",
                    "name": "scene-a",
                    "description": "test",
                    "log_token": "log",
                    "nbr_samples": 1,
                    "first_sample_token": sample["token"],
                    "last_sample_token": sample["token"],
                },
                "log": {
                    "token": "log",
                    "logfile": "logfile",
                    "vehicle": "car",
                    "date_captured": "2026-10-24",
                    "location": "test",
                },
                "map": {
                    "token": "map",
                    "category": "semantic_prior",
                    "filename": "maps/map.png",
                    "log_tokens": ["log"],
                },
                "map_storage_key": "g2/maps/map.png",
                "map_file_size": 20,
                "map_sha256": "e" * 64,
            },
        },
    )
    pose = SceneFramePose(
        scene_id=scene_id,
        frame_index=0,
        timestamp_us=110,
        ego_translation=[0, 0, 0],
        ego_rotation=[1, 0, 0, 0],
    )
    world.primary.scene_id = scene_id
    world.primary.frame_index = 0
    sensor_items = [("primary_lidar", world.primary), *world.cameras.items()]
    contracts = {"g2/maps/map.png": (20, "e" * 64)}
    for role, item in sensor_items:
        is_lidar = role == "primary_lidar"
        key = "g2/raw/0.pcd.bin" if is_lidar else item.file_path
        size = 20 if is_lidar else 37
        sha256 = "f" * 64 if is_lidar else "b" * 64
        sensor = {
            "token": "sensor-" + role,
            "channel": "LIDAR_TOP" if is_lidar else role.upper(),
            "modality": "lidar" if is_lidar else "camera",
        }
        calibrated = {
            "token": "calibrated-" + role,
            "sensor_token": sensor["token"],
            "translation": [0, 0, 0],
            "rotation": [1, 0, 0, 0],
            "camera_intrinsic": []
            if is_lidar
            else [[100, 0, 100], [0, 100, 60], [0, 0, 1]],
        }
        ego = {
            "token": "ego-" + role,
            "timestamp": 110,
            "translation": [0, 0, 0],
            "rotation": [1, 0, 0, 0],
        }
        sample_data = {
            "token": "sd-" + role,
            "sample_token": sample["token"],
            "calibrated_sensor_token": calibrated["token"],
            "ego_pose_token": ego["token"],
            "is_key_frame": True,
            "timestamp": 110,
            "prev": "",
            "next": "",
            "filename": "samples/LIDAR_TOP/0.pcd.bin"
            if is_lidar
            else "samples/" + role + "/0.jpg",
            "fileformat": "pcd" if is_lidar else "jpg",
            "width": 0 if is_lidar else item.width,
            "height": 0 if is_lidar else item.height,
        }
        source = {
            "sample": deepcopy(sample),
            "sensor": sensor,
            "calibrated_sensor": calibrated,
            "ego_pose": ego,
            "sample_data": sample_data,
            "source_storage_key": key,
            "source_file_size": size,
            "source_sha256": sha256,
        }
        if not is_lidar:
            source["platform_calibration_digest"] = calibration_digest(
                SensorCalibration.model_validate(calibration())
            )
        item.metadata_ = {**(item.metadata_ or {}), "nuscenes_export": source}
        contracts[key] = (size, sha256)
    world.view.scenes_by_id[scene_id] = scene
    world.view.poses_by_frame[(scene_id, 0)] = pose
    world.view.all_scene_tasks[scene_id] = [(world.task, world.primary)]
    for role, key in (("raw_lidar", "g2/raw/0.pcd.bin"), ("map", "g2/maps/map.png")):
        size, sha256 = contracts[key]
        world.snapshot["media"].append(
            {
                "task_id": str(world.task.id),
                "role": role,
                "source": {
                    "item_id": str(world.primary.id),
                    "object_key": key,
                    "size_bytes": size,
                    "sha256": sha256,
                },
            }
        )
    return contracts


@pytest.mark.parametrize(
    "g1", [("pointmask",), ("coco-multicamera",), ("kitti", "pointmask")]
)
async def test_mixed_nuscenes_preflight_uses_only_owned_metadata(world, g1):
    nuscenes(world)
    result = await report(world, (*g1, "nuscenes"))
    assert result.ready, result.model_dump()


@pytest.mark.parametrize(
    "fault", ["outside", "duplicate", "chain", "pose", "calibration"]
)
async def test_mixed_nuscenes_preserves_complete_scene_and_metadata_rules(
    world, monkeypatch, fault
):
    nuscenes(world)
    scene_id = world.primary.scene_id
    if fault in {"outside", "duplicate"}:
        extra = Task(id=uuid.uuid4(), project_id=world.project.id)
        world.view.all_scene_tasks[scene_id].append((extra, world.primary))
    elif fault == "chain":
        world.primary.metadata_["nuscenes_export"]["sample"]["next"] = "unexpected"
    elif fault == "pose":
        world.view.poses_by_frame[(scene_id, 0)].timestamp_us = 120
    else:
        world.cameras["camera_back"].metadata_["nuscenes_export"][
            "platform_calibration_digest"
        ] = "a" * 64
    expected = {
        "outside": "nuscenes_scene_incomplete",
        "duplicate": "nuscenes_scene_incomplete",
        "chain": "nuscenes_sample_chain_invalid",
        "pose": "nuscenes_frame_pose_drift",
        "calibration": "nuscenes_camera_calibration_drift",
    }[fault]
    assert expected in codes(await report(world, ("pointmask", "nuscenes")))


async def test_pure_nuscenes_uses_registered_collector_without_storage_io(world):
    nuscenes(world)
    result = await report(world, ("nuscenes",))
    assert result.ready, result.model_dump()
    assert world.calls[-1][1] == ["nuscenes"]


@pytest.fixture
async def registered_db(test_engine):
    """The integration owner provides the bounded, isolated collector schema."""
    from tests.test_lidar_export_inputs import input_db

    async for db in input_db.__wrapped__(test_engine):
        yield db


@pytest.mark.parametrize(
    "target,primary",
    [
        ("kitti", True),
        ("pointmask", True),
        ("coco-multicamera", True),
        ("coco-multicamera", False),
    ],
)
async def test_registered_preflight_real_database_canonical_link_only(
    registered_db, monkeypatch, target, primary
):
    from tests.test_lidar_export_inputs import setup

    db = registered_db
    project, dataset, _item, _asset, _replica, _task, cameras, _ann = await setup(
        db, direct=False, primary=primary
    )
    dataset.metadata_ = {"axis_convention": "iso_8855"}
    for camera, _asset, _replica, _link in cameras:
        camera.metadata_ = {"calibration": calibration()}
    await db.commit()
    monkeypatch.setattr(db, "commit", forbidden)
    monkeypatch.setattr(preflight.storage_service, "verify_upload", forbidden)
    monkeypatch.setattr(preflight.storage_service.client, "head_object", forbidden)
    monkeypatch.setattr(preflight.storage_service.client, "get_object", forbidden)
    before = tuple(db.dirty)
    result = await preflight.preflight_lidar_export(
        db,
        project_id=project.id,
        batch_id=None,
        targets=[target],
        options=LidarExportOptions(kitti_camera_role="camera_front"),
    )
    assert result.ready, result.model_dump()
    assert result.checked_tasks == 1 and tuple(db.dirty) == before
    assert db.in_transaction()


async def test_exact_frozen_worker_inputs_recheck_parent_rules_without_auxiliary_io(
    world, monkeypatch
):
    from app.services.exporting import lidar_frozen

    parent, _member, _track = manual_members(world)
    world.view.annotations.remove(parent)
    world.snapshot.update(
        project_id=str(world.project.id),
        batch_id=None,
        targets=["coco-multicamera", "nuscenes"],
        options={},
    )
    monkeypatch.setattr(lidar_frozen, "FrozenLidar", lambda snapshot: world.view)
    with pytest.raises(ContentError, match="multicamera_coco_parent_invalid"):
        await preflight.assert_frozen_lidar_ready(world.snapshot)
    assert world.calls == []


@pytest.mark.parametrize(
    "code", ["original_source_budget_exceeded", "original_relation_budget_exceeded"]
)
async def test_canonical_budget_failure_is_actionable_in_preflight(
    world, monkeypatch, code
):
    async def too_large(*args, **kwargs):
        raise ContentError(code, 422)

    monkeypatch.setattr(preflight, "_collect_registered", too_large)
    result = await report(world, ("pointmask",))
    assert not result.ready and codes(result) == {"lidar_export_input_budget_exceeded"}


@pytest.mark.parametrize("role", ["raw_lidar", "map"])
@pytest.mark.parametrize("damage", ["missing", "sha", "size", "key"])
async def test_nuscenes_never_heads_or_accepts_metadata_without_matching_receipt(
    world, role, damage
):
    nuscenes(world)
    binding = next(row for row in world.snapshot["media"] if row["role"] == role)
    if damage == "missing":
        world.snapshot["media"].remove(binding)
    else:
        field = {"sha": "sha256", "size": "size_bytes", "key": "object_key"}[damage]
        binding["source"][field] = 999 if damage == "size" else "changed"
    result = await report(world, ("nuscenes",))
    assert not result.ready
    assert "nuscenes_source_asset_drift" in codes(result)
