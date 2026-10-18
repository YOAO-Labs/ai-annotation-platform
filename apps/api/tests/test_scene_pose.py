"""Assets owner Scene pose mutations and trajectory API tests.

覆盖:upsert 幂等 / get_trajectory 有序 / 空 scene 降级(200 + poses=[])/
单帧查询 / Pydantic 长度校验拒非法 translation/rotation。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.db.models.dataset import Dataset
from aap_assets_catalog.spatial_values import FramePose
from aap_assets_catalog.spatial_contract import (
    CreateSceneRequest,
    ReadPosesRequest,
    UpsertPosesRequest,
)
from app.services.assets_spatial import AssetsSpatialGateway


async def _make_scene(db, owner_id, name="pose-scene"):
    ds = Dataset(
        display_id=f"DS-{uuid.uuid4().hex[:6]}",
        name=f"ds-{name}",
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    result = await AssetsSpatialGateway(db).create_scene(
        CreateSceneRequest(
            operation_id=uuid.uuid4(),
            dataset_id=ds.id,
            name=name,
            source_format=None,
            source_metadata={},
            created_by=owner_id,
        ),
        same_session_native=True,
    )
    return result.scene


def _pose(fi: int, x: float = 0.0, ts: int | None = None) -> FramePose:
    return FramePose(
        frame_index=fi,
        timestamp_us=ts,
        ego_translation=[x, 0.0, 0.0],
        ego_rotation=[1.0, 0.0, 0.0, 0.0],
    )


async def test_upsert_and_get_trajectory_ordered(db_session, super_admin):
    scene = await _make_scene(db_session, super_admin[0].id)
    # 乱序写入,读出按 frame_index 升序
    result = await AssetsSpatialGateway(db_session).upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(),
            scene_id=scene.id,
            poses=[
                _pose(2, x=10.0, ts=2_000_000),
                _pose(0, x=0.0, ts=1_000_000),
                _pose(1, x=5.0, ts=1_500_000),
            ],
        ),
        same_session_native=True,
    )
    assert result.upserted == 3

    traj = await AssetsSpatialGateway(db_session).read_poses(
        ReadPosesRequest(actor_id=None, scene_id=scene.id)
    )
    assert [p.frame_index for p in traj] == [0, 1, 2]
    assert [p.ego_translation[0] for p in traj] == [0.0, 5.0, 10.0]
    assert [p.timestamp_us for p in traj] == [1_000_000, 1_500_000, 2_000_000]


async def test_upsert_idempotent_updates_in_place(db_session, super_admin):
    scene = await _make_scene(db_session, super_admin[0].id)
    gateway = AssetsSpatialGateway(db_session)
    await gateway.upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(), scene_id=scene.id, poses=[_pose(0, x=1.0)]
        ),
        same_session_native=True,
    )
    # 同 (scene_id, frame_index) 重写 → 更新,不产生重复行
    await gateway.upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(), scene_id=scene.id, poses=[_pose(0, x=9.0, ts=42)]
        ),
        same_session_native=True,
    )

    traj = await gateway.read_poses(ReadPosesRequest(actor_id=None, scene_id=scene.id))
    assert len(traj) == 1
    assert traj[0].ego_translation[0] == 9.0
    assert traj[0].timestamp_us == 42


async def test_get_trajectory_empty_scene(db_session, super_admin):
    scene = await _make_scene(db_session, super_admin[0].id)
    assert (
        await AssetsSpatialGateway(db_session).read_poses(
            ReadPosesRequest(actor_id=None, scene_id=scene.id)
        )
        == []
    )


async def test_get_frame_pose_hit_and_miss(db_session, super_admin):
    scene = await _make_scene(db_session, super_admin[0].id)
    gateway = AssetsSpatialGateway(db_session)
    await gateway.upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(), scene_id=scene.id, poses=[_pose(3, x=7.0)]
        ),
        same_session_native=True,
    )
    hit = await gateway.read_frame_pose(scene.id, 3)
    assert hit is not None and hit.ego_translation[0] == 7.0
    miss = await gateway.read_frame_pose(scene.id, 4)
    assert miss is None


def test_frame_pose_validation_rejects_bad_lengths():
    with pytest.raises(ValidationError):
        FramePose(frame_index=0, ego_translation=[1.0, 2.0], ego_rotation=[1, 0, 0, 0])
    with pytest.raises(ValidationError):
        FramePose(frame_index=0, ego_translation=[1, 2, 3], ego_rotation=[1, 0, 0])


async def test_trajectory_api(db_session, httpx_client, super_admin):
    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    scene = await _make_scene(db_session, user.id)

    # 无位姿 scene → 200 + poses=[]
    resp = await httpx_client.get(
        f"/api/v1/scenes/{scene.id}/trajectory", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"scene_id": str(scene.id), "poses": []}

    await AssetsSpatialGateway(db_session).upsert_poses(
        UpsertPosesRequest(
            operation_id=uuid.uuid4(),
            scene_id=scene.id,
            poses=[_pose(1, x=5.0, ts=2), _pose(0, x=0.0, ts=1)],
        ),
        same_session_native=True,
    )
    resp = await httpx_client.get(
        f"/api/v1/scenes/{scene.id}/trajectory", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [p["frame_index"] for p in body["poses"]] == [0, 1]
    assert body["poses"][1]["ego_translation"] == [5.0, 0.0, 0.0]
    assert body["poses"][1]["ego_rotation"] == [1.0, 0.0, 0.0, 0.0]

    # 不存在的 scene → 404
    resp = await httpx_client.get(
        f"/api/v1/scenes/{uuid.uuid4()}/trajectory", headers=headers
    )
    assert resp.status_code == 404
