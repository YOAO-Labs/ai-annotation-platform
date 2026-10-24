"""Frozen LiDAR context produces actual ZIPs without live DB or raw storage I/O."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services.exporting import packaging
from app.services.exporting.lidar_media_paths import logical_sources
from app.services.exporting.lidar_pcd import inspect_pcd


def calibration():
    return {
        "extrinsic": [0, -1, 0, 0, 0, 0, -1, 0, 1, 0, 0, 0, 0, 0, 0, 1],
        "intrinsic": [100, 0, 100, 0, 100, 60, 0, 0, 1],
        "rect": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "packaging"
    root.mkdir()

    def forbidden(*args, **kwargs):
        pytest.fail("frozen packaging must not inspect live DB or raw storage")

    db = SimpleNamespace(get=forbidden, execute=forbidden)
    monkeypatch.setattr(packaging, "ExportService", forbidden)
    monkeypatch.setattr(packaging, "_load_lidar_link_items", forbidden)
    monkeypatch.setattr(packaging, "resolve_calibration_states", forbidden)
    monkeypatch.setattr(packaging, "_hash_dataset_object", forbidden)
    monkeypatch.setattr(packaging, "_read_dataset_object", forbidden)
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", forbidden)
    monkeypatch.setattr(
        packaging, "_new_zip_tempfile", lambda: str(root / "export.zip")
    )
    project = Project(
        id=uuid.uuid4(),
        display_id="P-FROZEN",
        name="Frozen LiDAR",
        data_type="lidar",
        type_key="lidar",
        type_label="LiDAR",
        owner_id=uuid.uuid4(),
        tool_bindings={
            "lidar_box_3d": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            },
            "point_mask_3d": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            },
        },
    )
    dataset_id, task_id = uuid.uuid4(), uuid.uuid4()
    primary = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="0001.pcd",
        file_path="saved-private-primary/0001.pcd",
        file_type="point_cloud",
        file_size=1,
        content_hash="md5-is-not-the-canonical-sha",
        metadata_={
            "storage_relative_path": "scene-a/lidar/0001.pcd",
            "point_count": 999,
        },
    )
    task = Task(
        id=task_id,
        project_id=project.id,
        dataset_item_id=primary.id,
        display_id="T-FROZEN",
        file_name=primary.file_name,
        file_path=primary.file_path,
        file_type="point_cloud",
    )
    linked, cameras = {}, {}
    for role in ("camera_front", "camera_back"):
        name = role.removeprefix("camera_")
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset_id,
            file_name=f"{name}.jpg",
            file_path=f"saved-private/{name}.jpg",
            file_type="image",
            file_size=999,
            width=200,
            height=120,
            metadata_={
                "storage_relative_path": f"scene-a/camera/{name}/0001.jpg",
                "calibration": calibration(),
            },
        )
        link = TaskDatasetItemLink(
            id=uuid.uuid4(),
            task_id=task.id,
            dataset_item_id=item.id,
            role=role,
            sensor_name=name,
        )
        linked[role], cameras[role] = (link, item), item
    linked["primary_lidar"] = (
        TaskDatasetItemLink(
            id=uuid.uuid4(),
            task_id=task.id,
            dataset_item_id=primary.id,
            role="primary_lidar",
        ),
        primary,
    )
    payload = (
        b"FIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        b"WIDTH 4\nHEIGHT 1\nPOINTS 4\nDATA ascii\n1 2 3\nnan 0 0\n4 5 6\n7 8 9\n"
    )
    data = {
        "primary_lidar": payload,
        "camera_front": b"exact front bytes",
        "camera_back": b"exact back bytes",
    }
    entries = {
        role: {
            "presigned_url": f"https://saved.invalid/fixed-owner/{role}?signature=fixed",
            "sha256": hashlib.sha256(value).hexdigest(),
            "size_bytes": len(value),
        }
        for role, value in data.items()
    }
    mask = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=project.owner_id,
        class_name="car",
        geometry={
            "type": "point_mask_3d",
            "point_indices": [1, 3],
            "source_point_count": 4,
            "decimate_stride": 9,
        },
    )
    state = SimpleNamespace(
        root=root,
        db=db,
        project=project,
        task=task,
        primary=primary,
        cameras=cameras,
        linked=linked,
        data=data,
        entries=entries,
        annotations=[mask],
        entry_calls=[],
        count_calls=[],
        pcd_calls=[],
        axis_calls=[],
        service_calls=[],
        axis="iso_8855",
        chunk_calls=[],
    )

    class FrozenService:
        def __init__(self):
            self.db = db

        async def iter_export_chunks(self, project_id, batch_id, *, with_annotations):
            state.chunk_calls.append((project_id, batch_id, with_annotations))
            items = {item.id: item for _, item in state.linked.values()}
            yield [state.task], {state.task.id: state.annotations}, items

        async def _axis_convention_by_task(self, *args):
            forbidden()

        async def export_aap_json(self, *args, **kwargs):
            return '{"frozen":true}'

    service = FrozenService()

    def frozen_service(actual_db):
        assert actual_db is db
        state.service_calls.append(True)
        return service

    def entry(identifier, role):
        assert identifier == task.id
        state.entry_calls.append(role)
        return {
            **state.entries[role],
            "expires_at": state.context.expires_at.isoformat(),
        }

    def point_count(identifier):
        state.count_calls.append(identifier)
        return inspect_pcd(io.BytesIO(state.data["primary_lidar"])).point_count

    def axis(identifier):
        state.axis_calls.append(identifier)
        return state.axis

    async def pcd_bytes(identifier):
        state.pcd_calls.append(identifier)
        return state.data["primary_lidar"]

    state.context = SimpleNamespace(
        project=project,
        service=frozen_service,
        entry=entry,
        point_count=point_count,
        axis_convention=axis,
        pcd_bytes=pcd_bytes,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        camera_members_by_task={},
        links_by_task={task.id: linked},
        calibration_states={},
        scenes_by_id={},
        poses_by_frame={},
    )
    try:
        yield state
    finally:
        shutil.rmtree(root)


@asynccontextmanager
async def archive(world, targets, *, use_context=True):
    path, count, size = await packaging.build_export_zip(
        world.db,
        world.project.id,
        batch_id=None,
        targets=targets,
        include_attributes=True,
        video_frame_mode="keyframes",
        format_options={"lidar": {"kitti_camera_role": "camera_front"}},
        lidar_media=world.context if use_context else None,
    )
    try:
        assert count >= 1 and size == Path(path).stat().st_size
        with zipfile.ZipFile(path) as result:
            yield result
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    "targets",
    [
        ["kitti"],
        ["pointmask"],
        ["coco-multicamera"],
        ["kitti", "pointmask"],
        ["kitti", "coco-multicamera"],
        ["pointmask", "coco-multicamera"],
        ["kitti", "pointmask", "coco-multicamera"],
    ],
)
async def test_real_zip_uses_only_frozen_saved_receipts_deadline_and_exact_original_points(
    world, targets
):
    if targets == ["kitti"]:
        del world.entries[
            "camera_back"
        ]  # Unselected cameras must not be signed or inspected.
    async with archive(world, targets) as result:
        for target in targets:
            prefix = target + "/" if len(targets) > 1 else ""
            if target == "coco-multicamera":
                manifest = json.loads(result.read(prefix + "media_manifest.json"))
                rows = manifest["media"]
                assert len(rows) == 2
                for row in rows:
                    expected = world.entries[row["sensor_role"]]
                    assert row["presigned_url"] == expected["presigned_url"]
                    assert row["source_sha256"] == expected["sha256"]
                    assert row["source_file_size"] == expected["size_bytes"]
                coco = json.loads(result.read(prefix + "annotations.json"))
                assert len(coco["images"]) == 2 and coco["annotations"] == []
            else:
                manifest = json.loads(result.read(prefix + "images_manifest.json"))
                rows = manifest["images"]
                assert {row["camera_role"] for row in rows} == (
                    {"camera_front"}
                    if target == "kitti"
                    else {"camera_front", "camera_back"}
                )
                for row in rows:
                    expected = world.entries[row["camera_role"]]
                    assert (row["presigned_url"], row["sha256"], row["size_bytes"]) == (
                        expected["presigned_url"],
                        expected["sha256"],
                        expected["size_bytes"],
                    )
                clouds = json.loads(result.read(prefix + "pointclouds_manifest.json"))
                cloud = clouds["pointclouds"][0]
                assert (
                    cloud["sha256"]
                    == hashlib.sha256(world.data["primary_lidar"]).hexdigest()
                )
                assert cloud["size_bytes"] == len(world.data["primary_lidar"])
                assert (
                    cloud["presigned_url"]
                    == world.entries["primary_lidar"]["presigned_url"]
                )
                assert clouds["expires_at"] == world.context.expires_at.isoformat()
                assert (
                    result.read(prefix + "fetch_images.py").decode()
                    == packaging._FETCH_LIDAR_IMAGES_TEMPLATE
                )
                if target == "pointmask":
                    labels = [
                        name
                        for name in result.namelist()
                        if name.startswith(prefix + "segmentation/")
                    ]
                    assert len(labels) == 1
                    assert struct.unpack("<4I", result.read(labels[0])) == (0, 1, 0, 1)
            assert manifest["expires_at"] == world.context.expires_at.isoformat()
            assert all(row["expires_at"] == manifest["expires_at"] for row in rows)
    assert world.service_calls == [True] and not world.pcd_calls
    assert bool(world.count_calls) == ("pointmask" in targets)
    assert bool(world.axis_calls) == ("kitti" in targets)


async def test_coco_without_primary_cloud_keeps_manual_stale_member_and_negative_camera(
    world,
):
    world.task.dataset_item_id = None
    world.linked.pop("primary_lidar")
    world.entries.pop("primary_lidar")
    member = Annotation(
        id=uuid.uuid4(),
        task_id=world.task.id,
        project_id=world.project.id,
        user_id=world.project.owner_id,
        source="manual",
        class_name="car",
        version=2,
        is_active=True,
        was_cancelled=False,
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        sensor_role="camera_front",
        sensor_dataset_item_id=world.cameras["camera_front"].id,
        scene_track_id=uuid.uuid4(),
        track_id="track-1",
        sensor_visibility="visible",
        calibration_revision=1,
        calibration_digest="a" * 64,
    )
    world.context.camera_members_by_task[world.task.id] = [member]
    world.context.calibration_states[member.sensor_dataset_item_id] = SimpleNamespace(
        revision=2, digest="b" * 64
    )
    async with archive(world, ["coco-multicamera"]) as result:
        coco = json.loads(result.read("annotations.json"))
        assert len(coco["images"]) == 2 and len(coco["annotations"]) == 1
        assert coco["annotations"][0]["relation_status"] == "stale"
        report = json.loads(result.read("export_report.json"))
        assert report["negative_images"] == 1 and report["stale_relation_count"] == 1
    assert not world.count_calls and not world.axis_calls and not world.pcd_calls
    assert set(world.entry_calls) == {"camera_front", "camera_back"}


async def test_task_original_pointmask_without_item_uses_task_basename_and_actual_count(
    world,
):
    world.task.dataset_item_id = None
    world.task.file_path = "task-originals/independent.pcd"
    world.linked.pop("primary_lidar")
    async with archive(world, ["pointmask"]) as result:
        manifest = json.loads(result.read("pointclouds_manifest.json"))
        assert manifest["pointclouds"][0]["dataset_id"] is None
        assert manifest["pointclouds"][0]["rel_path"] == "independent.pcd"
        labels = next(name for name in result.namelist() if name.endswith(".label"))
        assert len(result.read(labels)) == 16


async def test_omitted_context_preserves_legacy_manifest_and_transport_path(
    world, monkeypatch
):
    world.primary.metadata_["point_count"] = 4
    signed = []

    async def project(model, identifier):
        assert model is Project and identifier == world.project.id
        return world.project

    async def members(statement):
        return SimpleNamespace(scalars=lambda: [])

    async def links(service, tasks):
        return {world.task.id: world.linked}

    def signature(key, **kwargs):
        signed.append(key)
        return "legacy://" + key

    world.db.get, world.db.execute = project, members
    monkeypatch.setattr(
        packaging,
        "ExportService",
        lambda *args, **kwargs: world.context.service(world.db),
    )
    monkeypatch.setattr(packaging, "_load_lidar_link_items", links)
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", signature)
    async with archive(world, ["pointmask"], use_context=False) as result:
        manifest = json.loads(result.read("pointclouds_manifest.json"))
        assert "sha256" not in manifest["pointclouds"][0]
        assert manifest["pointclouds"][0]["presigned_url"].startswith("legacy://")
        assert datetime.fromisoformat(
            manifest["expires_at"]
        ) > world.context.expires_at + timedelta(days=6)
        assert (
            result.read("fetch_images.py").decode() == packaging._FETCH_IMAGES_TEMPLATE
        )
    assert set(signed) == {
        world.primary.file_path,
        *(item.file_path for item in world.cameras.values()),
    }
    assert not world.entry_calls and not world.count_calls


@pytest.mark.parametrize("axis", [None, "raw", "unknown"])
async def test_kitti_still_requires_canonical_trusted_axis_and_removes_failed_zip(
    world, axis
):
    world.axis = axis
    with pytest.raises(ValueError, match="axis_convention_untrusted"):
        async with archive(world, ["kitti"]):
            pytest.fail("untrusted axis must not create a successful archive")
    assert not (world.root / "export.zip").exists()


@pytest.mark.parametrize("targets", [["nuscenes"], ["pointmask", "nuscenes"]])
@pytest.mark.parametrize("empty", [False, True])
async def test_nuscenes_uses_owned_raw_map_cameras_and_frozen_pcd(
    world, monkeypatch, targets, empty
):
    world.data["primary_lidar"] = world.data["primary_lidar"].replace(
        b"nan 0 0", b"0 0 0"
    )
    if empty:
        world.data["primary_lidar"] = (
            "VERSION .7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nWIDTH 0\nHEIGHT 1\nPOINTS 0\nDATA ascii\n"
        ).encode()
        world.annotations = []
    world.entries["primary_lidar"].update(
        sha256=hashlib.sha256(world.data["primary_lidar"]).hexdigest(),
        size_bytes=len(world.data["primary_lidar"]),
    )
    for role, payload in (("raw_lidar", b"" if empty else b"r" * 80), ("map", b"map")):
        world.entries[role] = {
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "presigned_url": "https://saved.invalid/copies/" + role,
        }
    scene_id = uuid.uuid4()
    world.primary.scene_id, world.primary.frame_index = scene_id, 0
    world.primary.metadata_["nuscenes_export"] = {
        "source_file_size": 60,
        "sample": {"token": "original-sample"},
    }
    world.context.scenes_by_id[scene_id] = SimpleNamespace(
        id=scene_id,
        name="frozen scene",
        source_metadata={
            "nuscenes_export": {
                "map": {"filename": "maps/map.bin"},
                "map_storage_key": "g2/map.bin",
                "map_file_size": 3,
                "map_sha256": world.entries["map"]["sha256"],
            }
        },
    )
    world.context.poses_by_frame[(scene_id, 0)] = SimpleNamespace(
        timestamp_us=123,
        ego_translation=[0, 0, 0],
        ego_rotation=[1, 0, 0, 0],
    )
    for role, (_, item) in world.linked.items():
        item.metadata_["nuscenes_export"] = {
            **(item.metadata_.get("nuscenes_export") or {}),
            "sample_data": {"filename": f"samples/{role}.bin"},
            "calibrated_sensor": {},
            "sensor": {"modality": "lidar" if role == "primary_lidar" else "camera"},
            "ego_pose": {},
            "source_storage_key": f"g2/{role}.bin",
            "source_file_size": world.entries[
                "raw_lidar" if role == "primary_lidar" else role
            ]["size_bytes"],
            "source_sha256": world.entries[
                "raw_lidar" if role == "primary_lidar" else role
            ]["sha256"],
        }
    # Existing nuScenes counting stays intact; the PCD bytes come from its saved copy.
    frames = []

    def records(values):
        frames.extend(values)
        return {"sample": [{"token": "frozen-export"}]}

    monkeypatch.setattr(packaging, "build_nuscenes_frame_records", records)
    prefix = "nuscenes/" if len(targets) > 1 else ""
    async with archive(world, targets) as result:
        assert prefix + "v1.0-aap/sample.json" in result.namelist()
        assert not any("support/pointclouds" in name for name in result.namelist())
        manifest = json.loads(result.read(prefix + "media_manifest.json"))
        assert len(manifest["media"]) == 4
        assert {entry["presigned_url"] for entry in manifest["media"]} == {
            world.entries[role]["presigned_url"]
            for role in ("raw_lidar", "map", "camera_front", "camera_back")
        }
        assert b"--base-url" in result.read(prefix + "fetch_nuscenes_media.py")
        if "pointmask" in targets:
            assert len(
                result.read(
                    next(name for name in result.namelist() if name.endswith(".label"))
                )
            ) == (0 if empty else 16)
    assert world.pcd_calls == [world.task.id] and frames[0].scene_name == "frozen scene"
    assert {"raw_lidar", "map", "camera_front", "camera_back"}.issubset(
        world.entry_calls
    )


def snapshot(world, targets):
    return {
        "targets": targets,
        "options": {"lidar": {"kitti_camera_role": "camera_front"}},
        "tasks": [{"id": str(world.task.id), "file_path": world.task.file_path}],
        "items": [
            {
                "id": str(item.id),
                "file_path": item.file_path,
                "metadata_": deepcopy(item.metadata_),
            }
            for _, item in world.linked.values()
        ],
        "links": [
            {
                "id": str(link.id),
                "task_id": str(link.task_id),
                "dataset_item_id": str(item.id),
                "role": link.role,
                "sensor_name": link.sensor_name,
            }
            for link, item in world.linked.values()
        ],
        "media": [
            {
                "task_id": str(world.task.id),
                "role": role,
                "link_id": str(link.id) if role != "primary_lidar" else None,
                "source_key": hashlib.sha256(role.encode()).hexdigest(),
                "source": {
                    "item_id": str(item.id),
                    "object_key": "saved/opaque/object",
                },
            }
            for role, (link, item) in world.linked.items()
        ],
    }


@pytest.mark.parametrize(
    "targets",
    [
        ["kitti"],
        ["pointmask"],
        ["coco-multicamera"],
        ["kitti", "pointmask", "coco-multicamera", "nuscenes"],
    ],
)
def test_logical_sources_reproduces_actual_packaging_naming_and_private_support(
    world, targets
):
    frozen = snapshot(world, targets)
    mappings = logical_sources(frozen)
    roles = {entry["role"]: entry["source_key"] for entry in frozen["media"]}
    for target in targets:
        paths = mappings[target]
        if target == "nuscenes":
            assert paths == {
                f"support/pointclouds/{world.task.id}/scene-a/lidar/0001.pcd": roles[
                    "primary_lidar"
                ]
            }
        elif target == "coco-multicamera":
            assert paths == {
                f"images/{role}/{world.task.id}/{item.file_name}": roles[role]
                for role, item in world.cameras.items()
            }
        else:
            expected = {"velodyne/scene-a/lidar/0001.pcd": roles["primary_lidar"]}
            for role in ["camera_front"] if target == "kitti" else world.cameras:
                camera = role.removeprefix("camera_")
                expected[f"images/{camera}/scene-a/camera/{camera}/0001.jpg"] = roles[
                    role
                ]
            assert paths == expected


def test_logical_coco_has_no_primary_requirement_and_task_original_support_has_basename(
    world,
):
    frozen = snapshot(world, ["coco-multicamera"])
    frozen["media"] = [row for row in frozen["media"] if row["role"] != "primary_lidar"]
    assert len(logical_sources(frozen)["coco-multicamera"]) == 2
    frozen = snapshot(world, ["pointmask", "nuscenes"])
    primary = next(row for row in frozen["media"] if row["role"] == "primary_lidar")
    primary["source"]["item_id"] = None
    frozen["tasks"][0]["file_path"] = "task-originals/other.pcd"
    maps = logical_sources(frozen)
    assert "velodyne/other.pcd" in maps["pointmask"]
    assert f"support/pointclouds/{world.task.id}/other.pcd" in maps["nuscenes"]


@pytest.mark.parametrize("same_source", [True, False])
def test_duplicate_frame_labels_are_rejected_even_for_the_exact_same_source(
    world, same_source
):
    frozen = snapshot(world, ["pointmask"])
    frozen["links"] = []
    first = next(row for row in frozen["media"] if row["role"] == "primary_lidar")
    second = deepcopy(first)
    second["task_id"] = str(uuid.uuid4())
    second["source_key"] = first["source_key"] if same_source else "f" * 64
    frozen["tasks"].append({"id": second["task_id"], "file_path": world.task.file_path})
    frozen["media"] = [first, second]
    # Media may share an exact copy, but two Task labels still cannot occupy
    # one generated frame path. Their annotations could be different.
    with pytest.raises(ValueError, match="lidar_media_path_collision"):
        logical_sources(frozen)


def test_logical_paths_reject_unsafe_camera_names_and_ambiguous_binding(world):
    frozen = snapshot(world, ["pointmask"])
    frozen["links"][0]["sensor_name"] = "../escape"
    with pytest.raises(ValueError, match="lidar_media_path_invalid"):
        logical_sources(frozen)
    frozen = snapshot(world, ["pointmask"])
    frozen["media"].append(deepcopy(frozen["media"][0]))
    with pytest.raises(ValueError, match="lidar_media_binding_conflict"):
        logical_sources(frozen)


@pytest.mark.parametrize(
    "damage", ["sha256", "size_bytes", "expires_at", "presigned_url"]
)
def test_nuscenes_manifest_rejects_changed_saved_delivery_receipts(damage):
    saved = {
        "size_bytes": 80,
        "sha256": "a" * 64,
        "expires_at": "2030-01-01T00:00:00+00:00",
        "presigned_url": "https://saved.invalid/copy",
    }
    saved[damage] = None
    with pytest.raises(ValueError, match="nuscenes_delivery_receipt_changed"):
        packaging._trusted_media_entry(
            rel_path="samples/LIDAR_TOP/a.bin",
            saved=saved,
            kind="lidar",
            source_file_size=80,
            source_sha256="a" * 64,
            expires_iso="2030-01-01T00:00:00+00:00",
        )
