"""Auxiliary publication/ACL/source fences without model weights or live S3."""

from __future__ import annotations

import hashlib
import io
import uuid

import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from app.config import settings
from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    ProjectDataset,
    Scene,
    SensorCalibrationRevision,
)
from app.db.models.point_cloud_auxiliary import PointCloudAuxiliaryAsset
from app.db.models.task import Task
from app.schemas.point_cloud_auxiliary import PointCloudEnhancementConfig
from app.schemas.project import ProjectUpdate
from app.services.point_cloud_auxiliary import (
    AuxiliaryError,
    fail_generation,
    get_ready_generation,
    publish_auxiliary,
    reserve_generation,
    resolve_task_inputs,
)
from app.services.point_cloud_auxiliary.codec import encode_cloud
from app.services.point_cloud_auxiliary.calibration import (
    apply_calibration_repair,
    plan_calibration_repair,
)
from app.services.point_cloud_auxiliary.publication import (
    MODEL_SOURCE_COMMIT,
    WEIGHT_SHA256,
)
from app.services.point_cloud_auxiliary.sources import nuscenes_calibration
from app.services.sensor_calibration import (
    calibration_digest,
    resolve_calibration_state,
    update_calibration,
)
from app.services.storage import storage_service
from app.services.task_dataset_link import link_items
from tests.factory import create_project


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **extra):
        self.objects[(Bucket, Key)] = (bytes(Body), extra.get("Metadata", {}))

    def head_object(self, *, Bucket, Key):
        body, metadata = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(body),
            "Metadata": metadata,
            "ETag": hashlib.md5(body).hexdigest(),
        }

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}


@pytest.fixture
def memory_storage(monkeypatch):
    client = MemoryS3()
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(storage_service, "datasets_bucket", "auxiliary-test")
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda key, bucket=None: (
            client.head_object(Bucket=bucket or storage_service.bucket, Key=key)
            if (bucket or storage_service.bucket, key) in client.objects
            else None
        ),
    )
    monkeypatch.setattr(settings, "point_cloud_ai_enhancement_enabled", True)
    return client


def cloud():
    return {
        "xyz": np.array([[0.0, 0.0, 5.0], [1.0, 2.0, 8.0]], np.float32),
        "rgb": np.array([[255, 8, 4], [1, 2, 3]], np.uint8),
        "pixel": np.array([10, 20], np.int32),
        "confidence": np.array([0.8, 0.9], np.float32),
    }


def provenance(profile="omega-current-mono-512"):
    return {
        "producer": "unit-test",
        "profile": profile,
        "model_source_commit": MODEL_SOURCE_COMMIT,
        "weight_sha256": WEIGHT_SHA256,
        "preprocessing": {},
        "filtering": {},
        "calibration_policy": "frozen-test",
    }


async def seed(
    db, owner_id, client, *, camera_count=1, profile="omega-current-mono-512"
):
    project = await create_project(
        db, owner_id=owner_id, type_key="lidar", type_label="LiDAR"
    )
    project.data_type = "lidar"
    project.point_cloud_enhancement_config = {"enabled": True, "profile": profile}
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-AUX-{uuid.uuid4().hex[:8]}",
        name="aux-test",
        data_type="point_cloud",
        created_by=owner_id,
        metadata_={"axis_convention": "iso_8855"},
    )
    db.add(dataset)
    await db.flush()
    primary = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="0.pcd",
        file_path=f"{dataset.id}/0.pcd",
        file_type="point_cloud",
        content_hash=hashlib.sha256(b"primary-pcd").hexdigest(),
        file_size=11,
    )
    calibration = {
        "extrinsic": np.eye(4).reshape(-1).tolist(),
        "intrinsic": [100, 0, 50, 0, 100, 50, 0, 0, 1],
    }
    camera = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="0.jpg",
        file_path=f"{dataset.id}/0.jpg",
        file_type="image",
        content_hash=hashlib.sha256(b"camera-image").hexdigest(),
        file_size=12,
        width=100,
        height=100,
        metadata_={"calibration": calibration},
    )
    db.add_all(
        [primary, camera, ProjectDataset(project_id=project.id, dataset_id=dataset.id)]
    )
    await db.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=primary.id,
        display_id=f"T-AUX-{uuid.uuid4().hex[:8]}",
        file_name=primary.file_name,
        file_path=primary.file_path,
        file_type="point_cloud",
    )
    db.add(task)
    await db.flush()
    await link_items(
        db,
        task.id,
        [(primary.id, "primary_lidar", None), (camera.id, "camera_front", "front")],
    )
    for item, body in [(primary, b"primary-pcd"), (camera, b"camera-image")]:
        client.put_object(
            Bucket=storage_service.datasets_bucket,
            Key=item.file_path,
            Body=body,
            Metadata={"sha256": item.content_hash},
        )
    for index in range(1, camera_count):
        extra = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name=f"{index}.jpg",
            file_path=f"{dataset.id}/{index}.jpg",
            file_type="image",
            content_hash=camera.content_hash,
            file_size=camera.file_size,
            width=camera.width,
            height=camera.height,
            metadata_={"calibration": calibration},
        )
        db.add(extra)
        await db.flush()
        await link_items(
            db, task.id, [(extra.id, f"camera_view_{index}", f"view_{index}")]
        )
        client.put_object(
            Bucket=storage_service.datasets_bucket,
            Key=extra.file_path,
            Body=b"camera-image",
            Metadata={"sha256": extra.content_hash},
        )
    return project, dataset, task, primary, camera


def test_pcd_roundtrip_and_audit_are_row_aligned():
    source = cloud()
    pcd, sidecar, count = encode_cloud(source)
    header, body = pcd.split(b"DATA binary\n", 1)
    assert b"TYPE F F F F" in header
    rows = np.frombuffer(body, dtype=[("xyz", "<f4", 3), ("rgb", "<u4")])
    np.testing.assert_array_equal(rows["xyz"], source["xyz"])
    assert rows["rgb"].tolist() == [0xFF0804, 0x010203]
    with np.load(io.BytesIO(sidecar), allow_pickle=False) as audit:
        np.testing.assert_array_equal(audit["pixel"], source["pixel"])
    assert count == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("xyz", [[float("nan"), 1, 2], [1, 2, 3]]),
        ("rgb", np.ones((2, 3), np.float32)),
        ("pixel", np.ones(1, np.int32)),
    ],
)
def test_codec_rejects_corrupt_or_unaligned_data(field, value):
    source = cloud()
    source[field] = value
    with pytest.raises(AuxiliaryError):
        encode_cloud(source)


def test_config_defaults_and_null_guard():
    assert PointCloudEnhancementConfig().model_dump(mode="json") == {
        "schema_version": 1,
        "enabled": False,
        "profile": "omega-current-mono-512",
        "ml_backend_pool_id": None,
    }
    with pytest.raises(ValidationError):
        ProjectUpdate(point_cloud_enhancement_config=None)
    with pytest.raises(ValidationError):
        PointCloudEnhancementConfig(profile="unqualified-model")


def test_exposure_specific_calibration_uses_both_acquisition_poses():
    primary = {
        "sample": {"token": "same"},
        "ego_pose": {"rotation": [1, 0, 0, 0], "translation": [10, 0, 0]},
        "calibrated_sensor": {"rotation": [1, 0, 0, 0], "translation": [0, 0, 2]},
    }
    camera = {
        "sample": {"token": "same"},
        "ego_pose": {"rotation": [1, 0, 0, 0], "translation": [11, 0, 0]},
        "calibrated_sensor": {
            "rotation": [1, 0, 0, 0],
            "translation": [0, 1, 1],
            "camera_intrinsic": np.eye(3).tolist(),
        },
    }
    ego = np.array(nuscenes_calibration(primary, camera, "ego").extrinsic).reshape(4, 4)
    sensor = np.array(
        nuscenes_calibration(primary, camera, "sensor").extrinsic
    ).reshape(4, 4)
    np.testing.assert_array_equal(ego[:3, 3], [-1, -1, -1])
    np.testing.assert_array_equal(sensor[:3, 3], [-1, -1, 1])
    camera["sample"]["token"] = "other"
    with pytest.raises(AuxiliaryError, match="camera_acquisition_mismatch"):
        nuscenes_calibration(primary, camera, "ego")


async def test_publish_shares_owner_binding_and_is_idempotent(
    db_session, super_admin, memory_storage
):
    project, dataset, task, primary, camera = await seed(
        db_session, super_admin[0].id, memory_storage
    )
    inputs = await resolve_task_inputs(db_session, task)
    result = await publish_auxiliary(
        db_session, task, inputs, {"camera_front": cloud()}, provenance()
    )
    assert result.status == "ready" and result.generation == 1
    original_files = {
        key
        for key in memory_storage.objects
        if not key[1].startswith("point-cloud-auxiliary/")
    }
    assert len(original_files) == 2
    duplicate = await reserve_generation(db_session, task, inputs)
    assert duplicate.asset_id == result.asset_id and duplicate.generation == 1
    another = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=primary.id,
        display_id=f"T-AUX-{uuid.uuid4().hex[:8]}",
        file_name=primary.file_name,
        file_path=primary.file_path,
        file_type="point_cloud",
    )
    db_session.add(another)
    await db_session.flush()
    await link_items(
        db_session,
        another.id,
        [(primary.id, "primary_lidar", None), (camera.id, "camera_front", "front")],
    )
    _, shared, active = await get_ready_generation(db_session, another)
    assert shared.id == result.asset_id and active.generation == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(PointCloudAuxiliaryAsset)
        )
        == 1
    )


async def test_publication_fences_changed_source_and_keeps_prior_generation(
    db_session, super_admin, memory_storage
):
    _, _, task, _, camera = await seed(db_session, super_admin[0].id, memory_storage)
    first = await resolve_task_inputs(db_session, task)
    ready = await publish_auxiliary(
        db_session, task, first, {"camera_front": cloud()}, provenance()
    )
    current = await resolve_calibration_state(db_session, camera)
    changed = current.calibration.model_copy(deep=True)
    changed.extrinsic[3] += 0.5
    await update_calibration(
        db_session,
        dataset_item_id=camera.id,
        calibration=changed,
        expected_revision=current.revision,
        expected_digest=current.digest,
        actor_id=super_admin[0].id,
    )
    second = await resolve_task_inputs(db_session, task)
    reservation = await reserve_generation(db_session, task, second)
    assert reservation.generation == 2
    lease = reservation.lease_token
    memory_storage.put_object(
        Bucket=storage_service.datasets_bucket,
        Key=camera.file_path,
        Body=b"bad-image!!!",
        Metadata={"sha256": "0" * 64},
    )
    with pytest.raises(AuxiliaryError, match="source_hash_mismatch"):
        await publish_auxiliary(
            db_session,
            task,
            second,
            {"camera_front": cloud()},
            provenance(),
            generation=reservation,
        )
    asset = await db_session.get(PointCloudAuxiliaryAsset, ready.asset_id)
    assert asset.active_generation == 1
    assert await fail_generation(
        db_session, reservation.asset_id, 2, lease_token=lease, reason="source_changed"
    )
    assert reservation.status == "failed"


async def test_inflight_and_late_lease_cannot_publish(
    db_session, super_admin, memory_storage
):
    _, _, task, _, _ = await seed(db_session, super_admin[0].id, memory_storage)
    inputs = await resolve_task_inputs(db_session, task)
    reservation = await reserve_generation(db_session, task, inputs)
    with pytest.raises(AuxiliaryError, match="publication_in_progress"):
        await reserve_generation(db_session, task, inputs)
    lease = reservation.lease_token
    assert await fail_generation(
        db_session,
        reservation.asset_id,
        reservation.generation,
        lease_token=lease,
        reason="cancelled",
    )
    with pytest.raises(AuxiliaryError, match="publication_lease_lost"):
        await publish_auxiliary(
            db_session,
            task,
            inputs,
            {"camera_front": cloud()},
            provenance(),
            generation=reservation,
        )
    assert not await fail_generation(
        db_session,
        reservation.asset_id,
        reservation.generation,
        lease_token=lease,
        reason="late",
    )


async def test_read_contract_acl_expiry_corruption_and_revocation(
    db_session, super_admin, annotator, httpx_client, memory_storage, monkeypatch
):
    _, dataset, task, _, _ = await seed(db_session, super_admin[0].id, memory_storage)
    headers = {"Authorization": f"Bearer {super_admin[1]}"}
    base = f"/api/v1/tasks/{task.id}/point-cloud/augmentation"
    missing = await httpx_client.get(base, headers=headers)
    assert missing.status_code == 200 and missing.json()["status"] == "missing"
    inputs = await resolve_task_inputs(db_session, task)
    generation = await publish_auxiliary(
        db_session, task, inputs, {"camera_front": cloud()}, provenance()
    )
    ready = await httpx_client.get(base, headers=headers)
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert "key" not in ready.json()["cameras"][0]
    calls = []

    def signed(key, **kwargs):
        calls.append((key, kwargs))
        return "https://storage.test/short-lived"

    monkeypatch.setattr(storage_service, "generate_download_url", signed)
    payload = {
        "asset_id": str(generation.asset_id),
        "generation": 1,
        "camera_role": "camera_front",
    }
    url = await httpx_client.post(base + "/assets", headers=headers, json=payload)
    assert url.status_code == 200 and url.json()["expires_in"] == 300
    assert calls[0][1]["align"] is False and calls[0][1]["expires_in"] == 300
    forbidden = await httpx_client.get(
        base, headers={"Authorization": f"Bearer {annotator[1]}"}
    )
    assert forbidden.status_code in {403, 404}
    arbitrary = await httpx_client.post(
        base + "/assets",
        headers=headers,
        json={**payload, "key": "another-project/private.pcd"},
    )
    assert arbitrary.status_code == 422
    memory_storage.objects.pop(
        (storage_service.datasets_bucket, generation.cameras[0]["key"])
    )
    assert (await httpx_client.get(base, headers=headers)).json()["status"] == "failed"
    _, _, active = await get_ready_generation(db_session, task)
    assert active is None
    await db_session.execute(
        delete(ProjectDataset).where(ProjectDataset.dataset_id == dataset.id)
    )
    assert (await httpx_client.get(base, headers=headers)).json()[
        "reason"
    ] == "dataset_not_linked"
    assert (
        await httpx_client.post(base + "/assets", headers=headers, json=payload)
    ).status_code == 409


async def test_deployment_project_and_profile_gates(
    db_session, super_admin, memory_storage, httpx_client, monkeypatch
):
    project, _, task, _, _ = await seed(db_session, super_admin[0].id, memory_storage)
    inputs = await resolve_task_inputs(db_session, task)
    project.point_cloud_enhancement_config = {"enabled": False}
    await db_session.flush()
    with pytest.raises(AuxiliaryError, match="project_disabled"):
        await reserve_generation(db_session, task, inputs)
    project.point_cloud_enhancement_config = {"enabled": True}
    await db_session.flush()
    monkeypatch.setattr(settings, "point_cloud_ai_enhancement_enabled", False)
    with pytest.raises(AuxiliaryError, match="deployment_disabled"):
        await reserve_generation(db_session, task, inputs)
    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/point-cloud/augmentation",
        headers={"Authorization": f"Bearer {super_admin[1]}"},
    )
    assert (
        response.json()["status"] == "disabled" and response.json()["enabled"] is False
    )


async def test_create_and_clone_reset_experiment(
    db_session, super_admin, memory_storage, httpx_client
):
    project, _, _, _, _ = await seed(db_session, super_admin[0].id, memory_storage)
    headers = {"Authorization": f"Bearer {super_admin[1]}"}
    response = await httpx_client.post(
        "/api/v1/projects",
        headers=headers,
        json={
            "name": "clone-experiment",
            "type_label": "LiDAR",
            "data_type": "lidar",
            "source_project_id": str(project.id),
            "point_cloud_enhancement_config": {"enabled": True},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["point_cloud_enhancement_config"]["enabled"] is False


async def test_source_without_hash_and_cross_frame_binding_fail_closed(
    db_session, super_admin, memory_storage
):
    _, _, task, _, camera = await seed(db_session, super_admin[0].id, memory_storage)
    camera.content_hash = None
    await db_session.flush()
    with pytest.raises(AuxiliaryError, match="source_hash_missing"):
        await resolve_task_inputs(db_session, task)
    camera.content_hash = hashlib.sha256(b"camera-image").hexdigest()
    camera.frame_index = 8
    await db_session.flush()
    with pytest.raises(AuxiliaryError, match="camera_binding_mismatch"):
        await resolve_task_inputs(db_session, task)


async def legacy_nuscenes(db, owner_id, memory_storage):
    project, dataset, task, primary, camera = await seed(db, owner_id, memory_storage)
    scene = Scene(
        id=uuid.uuid4(),
        display_id=f"SC-AUX-{uuid.uuid4().hex[:8]}",
        dataset_id=dataset.id,
        name="legacy",
        source_format="nuscenes",
        source_metadata={"frame": "ego"},
    )
    db.add(scene)
    await db.flush()
    primary.scene_id = camera.scene_id = scene.id
    primary.frame_index = camera.frame_index = 0
    primary_source = {
        "sample": {"token": "same"},
        "ego_pose": {"rotation": [1, 0, 0, 0], "translation": [10, 0, 0]},
        "calibrated_sensor": {"rotation": [1, 0, 0, 0], "translation": [0, 0, 2]},
    }
    old = await resolve_calibration_state(db, camera)
    camera_source = {
        "sample": {"token": "same"},
        "ego_pose": {"rotation": [1, 0, 0, 0], "translation": [11, 0, 0]},
        "calibrated_sensor": {
            "rotation": [1, 0, 0, 0],
            "translation": [0, 1, 1],
            "camera_intrinsic": np.array(old.calibration.intrinsic)
            .reshape(3, 3)
            .tolist(),
        },
        "platform_calibration_digest": old.digest,
    }
    primary.metadata_ = {"nuscenes_export": primary_source}
    camera.metadata_ = {**camera.metadata_, "nuscenes_export": camera_source}
    await db.flush()
    return task, camera


async def test_coordinate_gate_and_append_only_baseline_repair(
    db_session, super_admin, memory_storage
):
    task, camera = await legacy_nuscenes(db_session, super_admin[0].id, memory_storage)
    old = await resolve_calibration_state(db_session, camera)
    with pytest.raises(AuxiliaryError, match="coordinate_calibration_mismatch"):
        await resolve_task_inputs(db_session, task)
    snapshot = await plan_calibration_repair(db_session, task)
    repaired = await apply_calibration_repair(
        db_session, task, snapshot, actor_id=super_admin[0].id
    )
    assert repaired["cameras"][0]["revision"] == 2
    rows = list(
        (
            await db_session.execute(
                select(SensorCalibrationRevision)
                .where(SensorCalibrationRevision.dataset_item_id == camera.id)
                .order_by(SensorCalibrationRevision.revision)
            )
        ).scalars()
    )
    assert len(rows) == 2 and rows[0].digest == old.digest
    assert (
        camera.metadata_["nuscenes_export"]["platform_calibration_digest"] == old.digest
    )
    inputs = await resolve_task_inputs(db_session, task)
    assert inputs.cameras[0].calibration_revision == 2
    assert inputs.cameras[0].calibration_digest == repaired["cameras"][0]["digest"]
    with pytest.raises(AuxiliaryError, match="calibration_repair_snapshot_changed"):
        await apply_calibration_repair(
            db_session, task, snapshot, actor_id=super_admin[0].id
        )


async def test_repair_preserves_manual_edit_and_rejects_missing_baseline(
    db_session, super_admin, memory_storage
):
    task, camera = await legacy_nuscenes(db_session, super_admin[0].id, memory_storage)
    current = await resolve_calibration_state(db_session, camera)
    changed = current.calibration.model_copy(deep=True)
    changed.extrinsic[3] = 0.25
    await update_calibration(
        db_session,
        dataset_item_id=camera.id,
        calibration=changed,
        expected_revision=current.revision,
        expected_digest=current.digest,
        actor_id=super_admin[0].id,
    )
    with pytest.raises(AuxiliaryError, match="manual_calibration_conflict"):
        await plan_calibration_repair(db_session, task)
    assert (
        await resolve_calibration_state(db_session, camera)
    ).digest == calibration_digest(changed)
    camera.metadata_ = {
        **camera.metadata_,
        "nuscenes_export": {
            key: value
            for key, value in camera.metadata_["nuscenes_export"].items()
            if key != "platform_calibration_digest"
        },
    }
    await db_session.flush()
    with pytest.raises(AuxiliaryError, match="manual_calibration_conflict"):
        await plan_calibration_repair(db_session, task)
