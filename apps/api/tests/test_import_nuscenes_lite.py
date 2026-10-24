"""v0.14.2 · import_nuscenes 的 lite 测试(不依赖真 4GB nuScenes 数据)。

在 tmp_path 造一个极小 fake nuScenes 根目录:2 个 scene、每 scene 3 个 sample、
1 个相机(CAM_FRONT)。验证:
  - 脚本完整跑通无异常;
  - DB 产生 2 个 Scene 行;
  - 每个 scene 的 lidar items frame_index = 0/1/2,scene_id 正确;
  - 跨 scene neighbors 不串(scene A 末帧 task 的 next 为空)。
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid
import zipfile

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.services import managed_original_creation as creation
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_managed_original_creation import Client, migration

from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.scene_pose import SceneFramePose
from app.db.models.task_batch import TaskBatch
from app.services import scene as scene_svc
from app.services.exporting.packaging import build_export_zip
from app.services.exporting.lidar_preflight import preflight_lidar_export
from app.services.storage import storage_service
from app.workers.export import _nuscenes_scope_digest
from scripts.import_nuscenes_scene import (
    _derived_display_id,
    _lidar_bin_to_binary_pcd,
    _load_table,
    _ordered_samples,
    _quat_wxyz_to_rot,
    _resolve_source_path,
    _safe_source_path,
    _transform,
    import_nuscenes,
)

pytestmark = pytest.mark.asyncio


async def test_derived_display_id_keeps_short_names_and_bounds_long_names():
    assert _derived_display_id("DS-NU-", "nu-lite") == "DS-NU-nu-lite"

    long_name = "nu-ego-0061-v0143"
    dataset_display_id = _derived_display_id("DS-NU-", long_name)
    project_display_id = _derived_display_id("P-NU-", long_name)

    assert len(dataset_display_id) <= 20
    assert len(project_display_id) <= 20
    assert dataset_display_id.startswith("DS-NU-")
    assert project_display_id.startswith("P-NU-")
    assert dataset_display_id == _derived_display_id("DS-NU-", long_name)
    assert project_display_id == _derived_display_id("P-NU-", long_name)


async def test_nuscenes_source_resolution_rejects_symlink_escape(tmp_path):
    root = tmp_path / "nuscenes"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"private")
    (root / "escape.bin").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes its root"):
        _resolve_source_path(root, "escape.bin")

    meta_dir = root / "v1.0-mini"
    meta_dir.mkdir()
    (meta_dir / "scene.json").symlink_to(outside)
    with pytest.raises(ValueError, match="metadata table escapes its root"):
        _load_table(meta_dir, "scene")


async def test_nuscenes_sample_chain_and_quaternion_reject_malformed_values():
    cyclic = {
        "token": "sample-0",
        "next": "sample-0",
    }
    with pytest.raises(ValueError, match="contains a cycle"):
        _ordered_samples(
            {"first_sample_token": "sample-0", "nbr_samples": 1},
            {"sample-0": cyclic},
        )
    with pytest.raises(ValueError, match="quaternion is invalid"):
        _quat_wxyz_to_rot([0.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="quaternion is invalid"):
        _quat_wxyz_to_rot([1e308, 1e308, 1e308, 1e308])


async def test_nuscenes_lidar_rejects_float32_overflow_after_transform(tmp_path):
    lidar_path = tmp_path / "lidar.pcd.bin"
    np.array([1.0, 2.0, 3.0, 0.5, 0.0], dtype=np.float32).tofile(lidar_path)
    transform = _transform(_IDENTITY_QUAT, [1e308, 0.0, 0.0])
    with pytest.raises(ValueError, match="non-finite points"):
        _lidar_bin_to_binary_pcd(lidar_path, transform=transform)


async def test_same_named_datasets_use_distinct_storage_roots(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=1)
    fake_client = _FakeS3Client()
    monkeypatch.setattr(storage_service, "client", fake_client)

    first = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="same-display-name",
        dataset_display_id="DS-NU-SAME-A",
        project_display_id="P-NU-SAME-A",
        owner_id=user.id,
    )
    second = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="same-display-name",
        dataset_display_id="DS-NU-SAME-B",
        project_display_id="P-NU-SAME-B",
        owner_id=user.id,
    )
    datasets = [
        await db_session.get(Dataset, result["dataset_id"])
        for result in (first, second)
    ]
    storage_roots = {
        dataset.metadata_["source"]["storage_root"] for dataset in datasets
    }
    assert len(storage_roots) == 2
    for dataset in datasets:
        root_prefix = f"__aap_managed_datasets__/{dataset.id}/"
        item_paths = list(
            (
                await db_session.execute(
                    select(DatasetItem.file_path).where(
                        DatasetItem.dataset_id == dataset.id
                    )
                )
            ).scalars()
        )
        assert item_paths
        assert all(path.startswith(root_prefix) for path in item_paths)

    first_dataset = datasets[0]
    original_metadata = first_dataset.metadata_
    first_dataset.metadata_ = {
        **original_metadata,
        "source": {
            **original_metadata["source"],
            "storage_root": datasets[1].metadata_["source"]["storage_root"],
        },
    }
    await db_session.flush()
    objects_before = deepcopy(fake_client.objects)
    with pytest.raises(ValueError, match="storage_root does not match its UUID"):
        await import_nuscenes(
            db_session,
            nuscenes_root=root,
            scene_names=["scene-0000"],
            dataset_name="same-display-name",
            dataset_display_id="DS-NU-SAME-A",
            project_display_id="P-NU-SAME-A",
            owner_id=user.id,
        )
    assert fake_client.objects == objects_before
    first_dataset.metadata_ = original_metadata
    await db_session.flush()


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.pcd.bin",
        r"..\escape.pcd.bin",
        "C:/escape.pcd.bin",
        r"\\server\share\escape.pcd.bin",
        "samples//escape.pcd.bin",
        "samples/./escape.pcd.bin",
    ],
)
async def test_safe_source_path_rejects_cross_platform_escape(filename):
    with pytest.raises(ValueError, match="source filename is unsafe"):
        _safe_source_path(filename)


# Controlled transport and independent ledgers live in one disposable namespace.
class _FakeS3Client(Client):
    def __init__(self):
        super().__init__()
        self.meta = SimpleNamespace(
            endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        )


@pytest.fixture(autouse=True)
def controlled_transport(monkeypatch):
    monkeypatch.setattr(creation, "create_client", lambda **_: storage_service.client)


@pytest.fixture
async def db_session(test_engine):
    schema = "nuscenes_lite_" + uuid.uuid4().hex
    excluded = {
        "alembic_version",
        "managed_original_creations",
        "dataset_auxiliary_originals",
    }
    async with test_engine.begin() as connection:
        source = await connection.scalar(text("SELECT current_schema()"))
        tables = list(
            await connection.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=:schema AND table_type='BASE TABLE'"
                ),
                {"schema": source},
            )
        )
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for table in tables:
            if table not in excluded:
                await connection.execute(
                    text(
                        f'CREATE TABLE "{schema}"."{table}" (LIKE "{source}"."{table}" INCLUDING ALL)'
                    )
                )
        for entity in ("tasks", "scenes"):
            await connection.execute(
                text(f'CREATE SEQUENCE "{schema}".display_seq_{entity}')
            )
        await connection.execute(
            text(
                f'ALTER TABLE "{schema}".scene_frame_poses ADD CONSTRAINT uq_scene_frame_pose UNIQUE (scene_id,frame_index)'
            )
        )
        for table, column in (
            ("dataset_items", "nuscenes_raw_original_id"),
            ("scenes", "nuscenes_map_original_id"),
            ("dataset_asset_locations", "creation_id"),
            ("managed_original_retirements", "creation_id"),
        ):
            await connection.execute(
                text(f'ALTER TABLE "{schema}"."{table}" DROP COLUMN IF EXISTS {column}')
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            for name in (
                "0219_managed_original_creations.py",
                "0230_managed_original_task_scope.py",
                "0235_dataset_auxiliary_originals.py",
            ):
                await connection.run_sync(
                    lambda sync, name=name: migration(sync, name).upgrade()
                )
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def _frozen_test_delivery(db, project_id, client):
    """Exercise frozen serialization; actual copy/lease behavior has worker tests."""
    from app.services.exporting import lidar_inputs
    from app.services.exporting.lidar_frozen import FrozenLidar

    captured = await lidar_inputs.collect(db, project_id, None, ["nuscenes"], {})

    class ReceiptView(FrozenLidar):
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        def entry(self, task_id, role):
            binding = next(
                row
                for row in captured.snapshot["media"]
                if row["task_id"] == str(task_id) and row["role"] == role
            )
            source = captured.sources[binding["source_key"]]
            data = client.objects[(source.bucket, source.object_key)]["data"]
            assert len(data) == source.size_bytes
            assert hashlib.sha256(data).hexdigest() == source.sha256
            return {
                "presigned_url": "https://receipt.invalid/" + binding["source_key"],
                "expires_at": self.expires_at.isoformat(),
                "sha256": source.sha256,
                "size_bytes": source.size_bytes,
            }

        async def pcd_bytes(self, task_id):
            binding = next(
                row
                for row in captured.snapshot["media"]
                if row["task_id"] == str(task_id) and row["role"] == "primary_lidar"
            )
            source = captured.sources[binding["source_key"]]
            data = client.objects[(source.bucket, source.object_key)]["data"]
            assert hashlib.sha256(data).hexdigest() == source.sha256
            return data

    return ReceiptView(captured.snapshot)


# --------------------------------------------------------------------------- #
# fake nuScenes 根目录构造
# --------------------------------------------------------------------------- #
_IDENTITY_QUAT = [1.0, 0.0, 0.0, 0.0]
_ZERO_TRANS = [0.0, 0.0, 0.0]
_INTRINSIC = [[1000.0, 0.0, 800.0], [0.0, 1000.0, 450.0], [0.0, 0.0, 1.0]]

# 两个 sensor:LIDAR_TOP + CAM_FRONT
_LIDAR_SENSOR = "sensor-lidar"
_CAM_SENSOR = "sensor-cam-front"
# 两个 calibrated_sensor(token 复用 sensor 名 + 后缀)
_CS_LIDAR = "cs-lidar"
_CS_CAM = "cs-cam-front"


def _write_fake_nuscenes(
    root: Path,
    *,
    scenes: int = 2,
    samples_per: int = 3,
    lidar_translation: list[float] | None = None,
) -> None:
    version = "v1.0-mini"
    meta = root / version
    meta.mkdir(parents=True)

    sensor = [
        {"token": _LIDAR_SENSOR, "channel": "LIDAR_TOP", "modality": "lidar"},
        {"token": _CAM_SENSOR, "channel": "CAM_FRONT", "modality": "camera"},
    ]
    calibrated_sensor = [
        {
            "token": _CS_LIDAR,
            "sensor_token": _LIDAR_SENSOR,
            "translation": lidar_translation or _ZERO_TRANS,
            "rotation": _IDENTITY_QUAT,
            "camera_intrinsic": [],
        },
        {
            "token": _CS_CAM,
            "sensor_token": _CAM_SENSOR,
            "translation": _ZERO_TRANS,
            "rotation": _IDENTITY_QUAT,
            "camera_intrinsic": _INTRINSIC,
        },
    ]

    scene_tbl: list[dict] = []
    sample_tbl: list[dict] = []
    sample_data_tbl: list[dict] = []
    ego_pose_tbl: list[dict] = []
    log_tbl = [
        {
            "token": "log-0",
            "logfile": "n015-2018-07-11-11-54-16+0800",
            "vehicle": "n015",
            "date_captured": "2018-07-11",
            "location": "singapore-onenorth",
        }
    ]
    map_tbl = [
        {
            "token": "map-0",
            "log_tokens": ["log-0"],
            "category": "semantic_prior",
            "filename": "maps/map-0.png",
        }
    ]

    # 造极小 lidar bin(3 点,每点 5 float)+ 1x1 jpg
    lidar_dir = root / "samples" / "LIDAR_TOP"
    cam_dir = root / "samples" / "CAM_FRONT"
    lidar_dir.mkdir(parents=True)
    cam_dir.mkdir(parents=True)

    pcd_bin = lidar_dir / "lidar.pcd.bin"
    np.array(
        [1, 2, 3, 0.5, 0, 4, 5, 6, 0.5, 1, 7, 8, 9, 0.5, 2],
        dtype=np.float32,
    ).tofile(str(pcd_bin))
    jpg_path = cam_dir / "cam.jpg"
    Image.new("RGB", (1, 1)).save(str(jpg_path), format="JPEG")
    map_path = root / "maps" / "map-0.png"
    map_path.parent.mkdir(parents=True)
    Image.new("L", (2, 2)).save(str(map_path), format="PNG")

    for s_i in range(scenes):
        sample_tokens = [f"sample-{s_i}-{i}" for i in range(samples_per)]
        scene_tbl.append(
            {
                "token": f"scene-token-{s_i}",
                "name": f"scene-{s_i:04d}",
                "description": "",
                "log_token": "log-0",
                "nbr_samples": samples_per,
                "first_sample_token": sample_tokens[0],
                "last_sample_token": sample_tokens[-1],
            }
        )
        for i, tok in enumerate(sample_tokens):
            sample_tbl.append(
                {
                    "token": tok,
                    "timestamp": (s_i * 100 + i) * 500000,
                    "prev": sample_tokens[i - 1] if i > 0 else "",
                    "next": sample_tokens[i + 1] if i < samples_per - 1 else "",
                    "scene_token": f"scene-token-{s_i}",
                }
            )
            ego_lidar = f"ego-lidar-{s_i}-{i}"
            ego_cam = f"ego-cam-{s_i}-{i}"
            # v0.15.0 · 每帧 ego 沿 x 直线前进 2m(造一段可断言差分的轨迹);
            # 同 sample 的 lidar/cam ego 取同值,首帧 calib 外参计算不受影响
            ego_translation = [(s_i * 100 + i) * 2.0, 0.0, 0.0]
            ego_pose_tbl.append(
                {
                    "token": ego_lidar,
                    "timestamp": (s_i * 100 + i) * 500000,
                    "rotation": _IDENTITY_QUAT,
                    "translation": ego_translation,
                }
            )
            ego_pose_tbl.append(
                {
                    "token": ego_cam,
                    "timestamp": (s_i * 100 + i) * 500000,
                    "rotation": _IDENTITY_QUAT,
                    "translation": ego_translation,
                }
            )
            sample_data_tbl.append(
                {
                    "token": f"sd-lidar-{s_i}-{i}",
                    "sample_token": tok,
                    "ego_pose_token": ego_lidar,
                    "calibrated_sensor_token": _CS_LIDAR,
                    "filename": "samples/LIDAR_TOP/lidar.pcd.bin",
                    "fileformat": "pcd",
                    "is_key_frame": True,
                    "height": 0,
                    "width": 0,
                    "timestamp": (s_i * 100 + i) * 500000,
                    "prev": "",
                    "next": "",
                }
            )
            sample_data_tbl.append(
                {
                    "token": f"sd-cam-{s_i}-{i}",
                    "sample_token": tok,
                    "ego_pose_token": ego_cam,
                    "calibrated_sensor_token": _CS_CAM,
                    "filename": "samples/CAM_FRONT/cam.jpg",
                    "fileformat": "jpg",
                    "is_key_frame": True,
                    "height": 1,
                    "width": 1,
                    "timestamp": (s_i * 100 + i) * 500000,
                    "prev": "",
                    "next": "",
                }
            )

    for name, rows in [
        ("scene", scene_tbl),
        ("sample", sample_tbl),
        ("sample_data", sample_data_tbl),
        ("calibrated_sensor", calibrated_sensor),
        ("sensor", sensor),
        ("ego_pose", ego_pose_tbl),
        ("log", log_tbl),
        ("map", map_tbl),
    ]:
        (meta / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")


async def test_import_nuscenes_two_scenes(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=2, samples_per=3)
    table = root / "v1.0-mini/sample_data.json"
    records = json.loads(table.read_text())
    for record in records:
        original = root / record["filename"]
        unique = original.with_name(record["token"] + "-" + original.name)
        shutil.copy2(original, unique)
        record["filename"] = unique.relative_to(root).as_posix()
    table.write_text(json.dumps(records))

    monkeypatch.setattr(storage_service, "client", _FakeS3Client())

    result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000", "scene-0001"],
        dataset_name="nu-lite-multi",
        owner_id=user.id,
        dataset_display_id="DS-PC-TEST",
        project_display_id="P-PC-TEST",
        project_name="nuScenes test seed",
        tool_bindings={"lidar_box_3d": {"enabled": True}},
    )

    # 1. 跑通,报告自洽
    assert len(result["scenes"]) == 2
    assert all(s["frames"] == 3 for s in result["scenes"])
    dataset_id = result["dataset_id"]
    dataset = await db_session.get(Dataset, dataset_id)
    project = await db_session.get(Project, result["project_id"])
    assert dataset.display_id == "DS-PC-TEST"
    assert dataset.file_count == 14
    assert dataset.metadata_["axis_convention"] == "iso_8855"
    assert dataset.metadata_["source"] == {
        "format": "nuscenes",
        "version": "v1.0-mini",
        "scenes": ["scene-0000", "scene-0001"],
        "pcd_encoding": "binary_xyz_f32",
        "storage_root": f"__aap_trusted_nuscenes__-{dataset.id.hex}",
        "storage_layout": "uuid_v1",
    }
    assert project.display_id == "P-PC-TEST"
    assert project.name == "nuScenes test seed"
    assert project.tool_bindings == {"lidar_box_3d": {"enabled": True}}
    items = list(
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
            )
        ).scalars()
    )
    assert len(items) == 14
    assert all(item.content_hash and len(item.content_hash) == 32 for item in items)
    assets = list(
        await db_session.scalars(
            select(DatasetAsset).where(
                DatasetAsset.dataset_item_id.in_([item.id for item in items])
            )
        )
    )
    assert len(assets) == len(items)
    assert all(
        asset.verification_state == "verified" and len(asset.checksum_value) == 64
        for asset in assets
    )
    camera_items = [item for item in items if item.file_type == "image"]
    assert camera_items
    assert all((item.width, item.height) == (1, 1) for item in camera_items)
    assert all(
        item.metadata_["nuscenes_export"]["sample_data"]["sample_token"]
        for item in camera_items
    )
    assert all(
        item.metadata_["nuscenes_export"]["sample"]["scene_token"]
        for item in camera_items
    )
    assert all(
        len(item.metadata_["nuscenes_export"]["source_sha256"]) == 64
        and item.metadata_["nuscenes_export"]["source_file_size"] > 0
        for item in camera_items
    )
    assert all(
        item.metadata_["nuscenes_export"]["platform_calibration_digest"]
        for item in camera_items
    )

    # 2. DB 里 2 个 Scene 行,name 对应
    scenes = (
        (
            await db_session.execute(
                select(Scene).where(Scene.dataset_id == dataset_id).order_by(Scene.name)
            )
        )
        .scalars()
        .all()
    )
    assert [s.name for s in scenes] == ["scene-0000", "scene-0001"]
    assert all(
        s.source_metadata["nuscenes_export"]["log"]["token"] == "log-0" for s in scenes
    )
    assert all(
        s.source_metadata["nuscenes_export"]["map"]["token"] == "map-0" for s in scenes
    )
    assert all(
        len(s.source_metadata["nuscenes_export"]["map_sha256"]) == 64
        and s.source_metadata["nuscenes_export"]["map_file_size"] > 0
        for s in scenes
    )

    # 3. 每个 scene 的 lidar items frame_index = 0/1/2,scene_id 正确
    for scene in scenes:
        lidar_rows = (
            (
                await db_session.execute(
                    select(DatasetItem)
                    .where(DatasetItem.scene_id == scene.id)
                    .where(DatasetItem.file_type == "point_cloud")
                    .order_by(DatasetItem.frame_index)
                )
            )
            .scalars()
            .all()
        )
        assert [r.frame_index for r in lidar_rows] == [0, 1, 2]
        assert all(r.scene_id == scene.id for r in lidar_rows)

    # 3.5 v0.15.0 · 每 scene 落 3 行 ego pose:frame_index 对齐、timestamp 单调、
    # translation 差分 = 设定步长(2m/帧)
    from app.services import scene_pose as scene_pose_svc

    for scene in scenes:
        traj = await scene_pose_svc.get_trajectory(db_session, scene.id)
        assert [p.frame_index for p in traj] == [0, 1, 2]
        ts = [p.timestamp_us for p in traj]
        assert ts == sorted(ts) and len(set(ts)) == 3
        xs = [p.ego_translation[0] for p in traj]
        assert xs[1] - xs[0] == pytest.approx(2.0)
        assert xs[2] - xs[1] == pytest.approx(2.0)
        assert all(p.ego_rotation == [1.0, 0.0, 0.0, 0.0] for p in traj)

    # 4. 跨 scene neighbors 不串:scene A 末帧 task 的 next 为空
    from app.db.models.task import Task

    scene_a = scenes[0]
    last_lidar = (
        await db_session.execute(
            select(DatasetItem)
            .where(DatasetItem.scene_id == scene_a.id)
            .where(DatasetItem.file_type == "point_cloud")
            .where(DatasetItem.frame_index == 2)
        )
    ).scalar_one()
    last_task = (
        await db_session.execute(
            select(Task).where(Task.dataset_item_id == last_lidar.id)
        )
    ).scalar_one()

    neighbors = await scene_svc.get_neighbors_for_task(
        db_session, task_id=last_task.id, k=1
    )
    assert neighbors is not None
    assert neighbors.scene_total_frames == 3
    assert neighbors.frame_index == 2
    assert neighbors.next == []  # 不串到 scene B 首帧
    assert len(neighbors.prev) == 1
    assert neighbors.prev[0].frame_index == 1

    preflight = await preflight_lidar_export(
        db_session,
        project_id=project.id,
        batch_id=None,
        targets=["nuscenes"],
        options=None,
    )
    assert preflight.ready is True
    assert preflight.checked_tasks == 6
    assert preflight.issues == []

    first_camera = camera_items[0]
    original_camera_metadata = first_camera.metadata_
    invalid_source = dict(original_camera_metadata["nuscenes_export"])
    invalid_source["source_sha256"] = "z" * 64
    first_camera.metadata_ = {
        **original_camera_metadata,
        "nuscenes_export": invalid_source,
    }
    await db_session.flush()
    invalid_sha_preflight = await preflight_lidar_export(
        db_session,
        project_id=project.id,
        batch_id=None,
        targets=["nuscenes"],
        options=None,
    )
    assert "nuscenes_source_asset_contract_missing" in {
        issue.code for issue in invalid_sha_preflight.issues
    }
    first_camera.metadata_ = original_camera_metadata
    await db_session.flush()

    digest_before_pose_change = await _nuscenes_scope_digest(
        db_session, project.id, None
    )
    first_pose = (
        await db_session.execute(
            select(SceneFramePose)
            .where(SceneFramePose.scene_id == scene_a.id)
            .order_by(SceneFramePose.frame_index)
            .limit(1)
        )
    ).scalar_one()
    original_translation = list(first_pose.ego_translation)
    first_pose.ego_translation = [
        original_translation[0] + 1.0,
        *original_translation[1:],
    ]
    await db_session.flush()
    assert (
        await _nuscenes_scope_digest(db_session, project.id, None)
        != digest_before_pose_change
    )
    first_pose.ego_translation = original_translation
    await db_session.flush()
    assert (
        await _nuscenes_scope_digest(db_session, project.id, None)
        == digest_before_pose_change
    )

    partial_batch = TaskBatch(
        project_id=project.id,
        dataset_id=dataset_id,
        display_id="B-NU-PARTIAL",
        name="partial scene",
        created_by=user.id,
    )
    db_session.add(partial_batch)
    await db_session.flush()
    partial_tasks = list(
        (
            await db_session.execute(
                select(Task)
                .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                .where(DatasetItem.scene_id == scene_a.id)
                .order_by(DatasetItem.frame_index)
                .limit(2)
            )
        ).scalars()
    )
    for task in partial_tasks:
        task.batch_id = partial_batch.id
    await db_session.flush()
    partial_preflight = await preflight_lidar_export(
        db_session,
        project_id=project.id,
        batch_id=partial_batch.id,
        targets=["nuscenes"],
        options=None,
    )
    assert partial_preflight.ready is False
    assert "nuscenes_scene_incomplete" in {
        issue.code for issue in partial_preflight.issues
    }

    monkeypatch.setattr(
        storage_service,
        "generate_download_url",
        lambda key, **_kwargs: f"https://example.invalid/{key}",
    )
    zip_path, file_count, _size = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        targets=["nuscenes"],
        include_attributes=True,
        video_frame_mode="keyframes",
        lidar_media=await _frozen_test_delivery(
            db_session, project.id, storage_service.client
        ),
    )
    materialized = tmp_path / "devkit-tree"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(materialized)
            assert {
                f"v1.0-aap/{name}.json"
                for name in (
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
                )
            }.issubset(archive.namelist())
            assert file_count == 6
        manifest = json.loads((materialized / "media_manifest.json").read_text())
        for media in manifest["media"]:
            source = root / media["rel_path"]
            destination = materialized / media["rel_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        from nuscenes.nuscenes import NuScenes
        from nuscenes.utils.data_classes import LidarPointCloud

        devkit = NuScenes(
            version="v1.0-aap",
            dataroot=str(materialized),
            verbose=False,
        )
        assert len(devkit.scene) == 2
        assert len(devkit.sample) == 6
        first_sample = devkit.get("sample", devkit.scene[0]["first_sample_token"])
        lidar_token = first_sample["data"]["LIDAR_TOP"]
        lidar_path = devkit.get_sample_data_path(lidar_token)
        assert LidarPointCloud.from_file(lidar_path).points.shape[0] == 4
    finally:
        os.unlink(zip_path)


async def test_backfill_frame_poses_restores_deleted_rows(
    tmp_path, db_session, super_admin, monkeypatch
):
    """v0.15.0 · backfill 脚本:模拟 v0.15.0 前导入的库(无 pose 行)→ 补齐。"""
    from sqlalchemy import delete

    from app.db.models.scene_pose import SceneFramePose
    from app.services import scene_pose as scene_pose_svc
    from scripts.backfill_frame_poses import backfill_frame_poses

    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=3)
    monkeypatch.setattr(storage_service, "client", _FakeS3Client())

    result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-bf",
        owner_id=user.id,
    )
    await db_session.execute(delete(SceneFramePose))

    report = await backfill_frame_poses(
        db_session,
        dataset_ref=str(result["dataset_id"]),
        nuscenes_root=root,
    )
    assert report == [{"name": "scene-0000", "poses": 3}]

    scene = (
        await db_session.execute(
            select(Scene).where(Scene.dataset_id == result["dataset_id"])
        )
    ).scalar_one()
    traj = await scene_pose_svc.get_trajectory(db_session, scene.id)
    assert [p.frame_index for p in traj] == [0, 1, 2]
    assert [p.ego_translation[0] for p in traj] == [0.0, 2.0, 4.0]

    # display_id 引用同样可用,且 upsert 幂等(重跑不翻倍)
    report2 = await backfill_frame_poses(
        db_session, dataset_ref="DS-NU-nu-lite-bf", nuscenes_root=root
    )
    assert report2 == [{"name": "scene-0000", "poses": 3}]
    assert len(await scene_pose_svc.get_trajectory(db_session, scene.id)) == 3


async def test_manifest_exposes_ego_pose(
    tmp_path, db_session, httpx_client, super_admin, monkeypatch
):
    """v0.15.0 · manifest 透出本帧 ego_pose(无位姿时为 null,此处验证有值路径)。"""
    from app.db.models.task import Task

    user, token = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=2)
    monkeypatch.setattr(storage_service, "client", _FakeS3Client())
    monkeypatch.setattr(
        storage_service,
        "generate_download_url",
        lambda key, **kw: f"https://fake/{key}",
    )

    result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-mani",
        owner_id=user.id,
    )

    frame1_item = (
        await db_session.execute(
            select(DatasetItem)
            .where(DatasetItem.dataset_id == result["dataset_id"])
            .where(DatasetItem.file_type == "point_cloud")
            .where(DatasetItem.frame_index == 1)
        )
    ).scalar_one()
    task = (
        await db_session.execute(
            select(Task).where(Task.dataset_item_id == frame1_item.id)
        )
    ).scalar_one()

    resp = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["frame_index"] == 1
    assert body["ego_pose"]["frame_index"] == 1
    assert body["ego_pose"]["ego_translation"] == [2.0, 0.0, 0.0]
    assert body["ego_pose"]["ego_rotation"] == [1.0, 0.0, 0.0, 0.0]
    assert body["ego_pose"]["timestamp_us"] == 500000


async def test_import_nuscenes_bounds_long_derived_display_ids(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=1)
    monkeypatch.setattr(storage_service, "client", _FakeS3Client())

    dataset_name = "nu-ego-0061-v0143"
    result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name=dataset_name,
        owner_id=user.id,
    )

    dataset = await db_session.get(Dataset, result["dataset_id"])
    project = await db_session.get(Project, result["project_id"])

    assert dataset.display_id == _derived_display_id("DS-NU-", dataset_name)
    assert project.display_id == _derived_display_id("P-NU-", dataset_name)
    assert len(dataset.display_id) <= 20
    assert len(project.display_id) <= 20


async def test_import_nuscenes_frame_modes_axis_and_points(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(
        root,
        scenes=1,
        samples_per=1,
        lidar_translation=[10.0, 20.0, 30.0],
    )

    fake_client = _FakeS3Client()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ego_result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-ego",
        owner_id=user.id,
        frame="ego",
    )
    sensor_result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-sensor",
        owner_id=user.id,
        frame="sensor",
    )

    ego_dataset = await db_session.get(Dataset, ego_result["dataset_id"])
    sensor_dataset = await db_session.get(Dataset, sensor_result["dataset_id"])
    assert ego_dataset.metadata_["axis_convention"] == "iso_8855"
    assert sensor_dataset.metadata_["axis_convention"] == "apollo"

    async def first_point(dataset: Dataset) -> tuple[float, float, float]:
        key = await db_session.scalar(
            select(DatasetItem.file_path).where(
                DatasetItem.dataset_id == dataset.id,
                DatasetItem.file_type == "point_cloud",
            )
        )
        payload = fake_client.objects[(storage_service.datasets_bucket, key)]["data"]
        marker = b"DATA binary\n"
        offset = payload.index(marker) + len(marker)
        return tuple(np.frombuffer(payload[offset : offset + 12], dtype="<f4"))

    assert await first_point(ego_dataset) == pytest.approx((11.0, 22.0, 33.0))
    assert await first_point(sensor_dataset) == pytest.approx((1.0, 2.0, 3.0))


async def test_existing_ascii_scene_is_upgraded_in_place(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=1)
    client = _FakeS3Client()
    monkeypatch.setattr(storage_service, "client", client)
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=_derived_display_id("DS-NU-", "nu-lite-upgrade"),
        name="nu-lite-upgrade",
        data_type="point_cloud",
        is_temporal=True,
        created_by=user.id,
        metadata_={
            "axis_convention": "iso_8855",
            "source": {
                "storage_layout": "legacy_name",
                "storage_root": "nu-lite-upgrade",
            },
        },
    )
    db_session.add(dataset)
    await db_session.flush()
    scene = await scene_svc.create_scene(
        db_session,
        dataset_id=dataset.id,
        name="scene-0000",
        source_format="nuscenes",
        source_metadata={"frame": "ego"},
        created_by=user.id,
    )
    legacy = (
        b"# .PCD v0.7 - Point Cloud Data file format\n"
        b"VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        b"WIDTH 3\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS 3\nDATA ascii\n"
        b"1 2 3\n4 5 6\n7 8 9\n"
    )
    items = []
    for logical, kind, payload in (
        ("scene-0000/lidar/scene-0000_000000.pcd", "point_cloud", legacy),
        (
            "scene-0000/camera/CAM_FRONT/scene-0000_000000.jpg",
            "image",
            (root / "samples/CAM_FRONT/cam.jpg").read_bytes(),
        ),
    ):
        path = "nu-lite-upgrade/" + logical
        client.store(storage_service.datasets_bucket, path, payload)
        row = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            scene_id=scene.id,
            frame_index=0,
            file_name=Path(path).name,
            file_path=path,
            file_type=kind,
            file_size=len(payload),
            content_hash=hashlib.sha256(payload).hexdigest(),
            metadata_={"storage_relative_path": logical},
        )
        db_session.add(row)
        items.append(row)
    await db_session.flush()
    item = items[0]
    old_id, old_key = item.id, item.file_path
    result = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-upgrade",
        owner_id=user.id,
    )
    assert result["scenes"] == [
        {
            "name": "scene-0000",
            "frames": 1,
            "skipped": True,
            "upgraded_point_clouds": 1,
            "backfilled_camera_dimensions": 1,
        }
    ]
    assert item.id == old_id and item.file_path != old_key
    assert client.objects[(storage_service.datasets_bucket, old_key)]["data"] == legacy
    upgraded = client.objects[(storage_service.datasets_bucket, item.file_path)]["data"]
    assert b"DATA binary\n" in upgraded[:200]
    assert item.file_size == len(upgraded)
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    assert asset.checksum_value == hashlib.sha256(upgraded).hexdigest()
    assert item.nuscenes_raw_original_id is not None
    assert dataset.metadata_["source"]["pcd_encoding"] == "binary_xyz_f32"
    assert dataset.metadata_["source"]["storage_layout"] == "legacy_name"
    keys = set(client.objects)
    third = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-upgrade",
        owner_id=user.id,
    )
    assert third["scenes"] == [{"name": "scene-0000", "frames": 1, "skipped": True}]
    assert set(client.objects) == keys


async def test_existing_scene_rejects_source_drift_before_object_writes(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=1)
    fake_client = _FakeS3Client()
    monkeypatch.setattr(storage_service, "client", fake_client)

    await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-source-drift",
        owner_id=user.id,
    )
    objects_before = deepcopy(fake_client.objects)
    lidar_path = root / "samples" / "LIDAR_TOP" / "lidar.pcd.bin"
    original_lidar = lidar_path.read_bytes()
    lidar_path.write_bytes(original_lidar + (b"\0" * 20))

    with pytest.raises(ValueError, match="existing scene source drift"):
        await import_nuscenes(
            db_session,
            nuscenes_root=root,
            scene_names=["scene-0000"],
            dataset_name="nu-lite-source-drift",
            owner_id=user.id,
        )
    assert fake_client.objects == objects_before

    lidar_path.write_bytes(original_lidar)
    calibration_path = root / "v1.0-mini" / "calibrated_sensor.json"
    calibration_rows = json.loads(calibration_path.read_text(encoding="utf-8"))
    calibration_rows[0]["translation"] = [9.0, 0.0, 0.0]
    calibration_path.write_text(json.dumps(calibration_rows), encoding="utf-8")
    with pytest.raises(ValueError, match="existing scene source drift"):
        await import_nuscenes(
            db_session,
            nuscenes_root=root,
            scene_names=["scene-0000"],
            dataset_name="nu-lite-source-drift",
            owner_id=user.id,
        )
    assert fake_client.objects == objects_before

    calibration_rows[0]["translation"] = _ZERO_TRANS
    calibration_path.write_text(json.dumps(calibration_rows), encoding="utf-8")
    scene_path = root / "v1.0-mini" / "scene.json"
    scene_rows = json.loads(scene_path.read_text(encoding="utf-8"))
    scene_rows[0]["token"] = "replacement-scene-token"
    scene_path.write_text(json.dumps(scene_rows), encoding="utf-8")
    with pytest.raises(ValueError, match="existing scene source drift"):
        await import_nuscenes(
            db_session,
            nuscenes_root=root,
            scene_names=["scene-0000"],
            dataset_name="nu-lite-source-drift",
            owner_id=user.id,
        )
    assert fake_client.objects == objects_before


async def test_existing_binary_scene_reuses_platform_pcd_receipt_without_rewrite(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=1)
    client = _FakeS3Client()
    monkeypatch.setattr(storage_service, "client", client)
    first = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-pcd-fingerprint",
        owner_id=user.id,
    )
    item = await db_session.scalar(
        select(DatasetItem).where(
            DatasetItem.dataset_id == first["dataset_id"],
            DatasetItem.file_type == "point_cloud",
        )
    )
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
    )
    replica = await db_session.scalar(
        select(DatasetAssetLocation).where(DatasetAssetLocation.asset_id == asset.id)
    )
    before = (
        item.file_path,
        asset.checksum_value,
        asset.size_bytes,
        replica.creation_id,
        replica.etag,
        replica.verified_at,
    )
    objects_before = deepcopy(client.objects)
    writes_before = [
        call
        for call in client.calls
        if call[0] in {"create", "part", "copy", "complete", "put"}
    ]
    second = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-pcd-fingerprint",
        owner_id=user.id,
    )
    assert (
        item.file_path,
        asset.checksum_value,
        asset.size_bytes,
        replica.creation_id,
        replica.etag,
        replica.verified_at,
    ) == before
    assert client.objects == objects_before
    assert [
        call
        for call in client.calls
        if call[0] in {"create", "part", "copy", "complete", "put"}
    ] == writes_before
    assert second["scenes"] == [{"name": "scene-0000", "frames": 1, "skipped": True}]


async def test_existing_scene_backfills_missing_camera_dimensions(
    tmp_path, db_session, super_admin, monkeypatch
):
    user, _ = super_admin
    root = tmp_path / "nuscenes-mini"
    _write_fake_nuscenes(root, scenes=1, samples_per=2)
    monkeypatch.setattr(storage_service, "client", _FakeS3Client())

    first = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-dimensions",
        owner_id=user.id,
    )
    camera_items = list(
        (
            await db_session.execute(
                select(DatasetItem)
                .where(DatasetItem.dataset_id == first["dataset_id"])
                .where(DatasetItem.file_type == "image")
            )
        ).scalars()
    )
    assert len(camera_items) == 2
    for item in camera_items:
        item.width = None
        item.height = None
    await db_session.flush()

    second = await import_nuscenes(
        db_session,
        nuscenes_root=root,
        scene_names=["scene-0000"],
        dataset_name="nu-lite-dimensions",
        owner_id=user.id,
    )

    assert [(item.width, item.height) for item in camera_items] == [(1, 1), (1, 1)]
    assert second["scenes"] == [
        {
            "name": "scene-0000",
            "frames": 2,
            "skipped": True,
            "backfilled_camera_dimensions": 2,
        }
    ]
