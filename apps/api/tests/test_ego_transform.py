"""v0.15.1 · ego_transform 几何纯函数测试。

覆盖:世界位置守恒(静止物补偿后 world 坐标不变)/ 插值中点 /
slerp 朝向单位四元数 / 缺 pose 恒等降级。
"""

from __future__ import annotations

import math

import pytest

from aap_assets_catalog.spatial_values import FramePose
from aap_assets_catalog.axis_convention import _euler_xyz_to_mat3, _mat_vec
from app.services.ego_transform import (
    _mat3_to_quat_wxyz,
    _quat_wxyz_to_mat3,
    _slerp_wxyz,
    box_ego_to_world,
    compensate_psr,
    interpolate_psr,
)


def _pose(fi: int, x: float = 0.0, y: float = 0.0, yaw: float = 0.0) -> FramePose:
    """ego→global:绕 z 转 yaw + 平移 (x,y,0)。四元数 [w,x,y,z]。"""
    return FramePose(
        frame_index=fi,
        ego_translation=[x, y, 0.0],
        ego_rotation=[math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)],
    )


_PSR = {
    "center": [10.0, 2.0, 1.0],
    "size": [4.0, 2.0, 1.5],
    "rotation": [0.0, 0.0, 0.3],
}


def test_compensate_world_position_invariant():
    """静止物:补偿后框在两帧的世界坐标完全一致(造平移 + 小转向)。"""
    pose_i = _pose(0, x=0.0, y=0.0, yaw=0.0)
    pose_j = _pose(1, x=5.0, y=1.0, yaw=0.2)

    psr_j, compensated = compensate_psr(_PSR, pose_src=pose_i, pose_dst=pose_j)
    assert compensated is True

    cw_i, rw_i = box_ego_to_world(_PSR, pose_i)
    cw_j, rw_j = box_ego_to_world(psr_j, pose_j)
    assert cw_j == pytest.approx(cw_i, abs=1e-9)
    assert rw_j == pytest.approx(rw_i, abs=1e-9)
    assert psr_j["size"] == _PSR["size"]
    # 车前进 5m → 静止物在新 ego 系里应"后退"(x 变小),不是原样复制
    assert psr_j["center"][0] < _PSR["center"][0]


def test_compensate_pure_translation_math():
    """无旋转 ego 平移 dx → 框 ego 系 x 坐标精确减 dx,旋转不变。"""
    psr_j, _ = compensate_psr(_PSR, pose_src=_pose(0, x=0.0), pose_dst=_pose(1, x=3.0))
    assert psr_j["center"] == pytest.approx([7.0, 2.0, 1.0])
    assert psr_j["rotation"] == pytest.approx([0.0, 0.0, 0.3])


def test_compensate_missing_pose_degrades_to_identity():
    for src, dst in [(None, _pose(1)), (_pose(0), None), (None, None)]:
        psr_j, compensated = compensate_psr(_PSR, pose_src=src, pose_dst=dst)
        assert compensated is False
        assert psr_j["center"] == pytest.approx(_PSR["center"])
        assert psr_j["rotation"] == pytest.approx(_PSR["rotation"])
        assert psr_j["size"] == pytest.approx(_PSR["size"])


def test_interpolate_midpoint_world_center():
    """t=0.5 的世界中心 = 两端世界中心的中点;尺寸线性插值。"""
    pose_a, pose_b = _pose(0, x=0.0), _pose(2, x=10.0)
    pose_m = _pose(1, x=4.0)  # 中间帧 ego 不在正中,验证投影正确性
    psr_a = {
        "center": [10.0, 0.0, 0.0],
        "size": [4.0, 2.0, 1.5],
        "rotation": [0.0, 0.0, 0.0],
    }
    psr_b = {
        "center": [4.0, 0.0, 0.0],
        "size": [5.0, 2.0, 1.5],
        "rotation": [0.0, 0.0, 0.0],
    }

    psr_m, compensated = interpolate_psr(
        psr_a, psr_b, 0.5, pose_a=pose_a, pose_b=pose_b, pose_mid=pose_m
    )
    assert compensated is True
    # 世界中心:a=(10,0,0), b=(14,0,0) → 中点 (12,0,0);pose_m 平移 4 → ego x=8
    cw_m, _ = box_ego_to_world(psr_m, pose_m)
    assert cw_m == pytest.approx((12.0, 0.0, 0.0))
    assert psr_m["center"] == pytest.approx([8.0, 0.0, 0.0])
    assert psr_m["size"] == pytest.approx([4.5, 2.0, 1.5])


def test_interpolate_slerp_yaw_midpoint():
    """朝向 slerp:0 → 0.6 rad 的 t=0.5 中点 = 0.3 rad(同帧 pose 下退化为纯朝向插值)。"""
    pose = _pose(0)
    psr_a = dict(_PSR, rotation=[0.0, 0.0, 0.0])
    psr_b = dict(_PSR, rotation=[0.0, 0.0, 0.6])
    psr_m, _ = interpolate_psr(
        psr_a, psr_b, 0.5, pose_a=pose, pose_b=pose, pose_mid=pose
    )
    assert psr_m["rotation"][2] == pytest.approx(0.3)


def test_interpolate_missing_pose_degrades_to_ego_lerp():
    psr_a = {
        "center": [0.0, 0.0, 0.0],
        "size": [4.0, 2.0, 1.5],
        "rotation": [0.0, 0.0, 0.0],
    }
    psr_b = {
        "center": [8.0, 4.0, 2.0],
        "size": [4.0, 2.0, 1.5],
        "rotation": [0.0, 0.0, 0.4],
    }
    psr_m, compensated = interpolate_psr(
        psr_a, psr_b, 0.25, pose_a=None, pose_b=_pose(2), pose_mid=None
    )
    assert compensated is False
    assert psr_m["center"] == pytest.approx([2.0, 1.0, 0.5])
    assert psr_m["rotation"][2] == pytest.approx(0.1)


def test_quat_mat_roundtrip_and_slerp_unit_norm():
    for yaw in (0.0, 0.7, -2.0, 3.0):
        m = _euler_xyz_to_mat3(0.1, -0.2, yaw)
        q = _mat3_to_quat_wxyz(m)
        assert sum(v * v for v in q) == pytest.approx(1.0)
        assert _quat_wxyz_to_mat3(q) == pytest.approx(m, abs=1e-9)

    qa = _mat3_to_quat_wxyz(_euler_xyz_to_mat3(0, 0, 0.0))
    qb = _mat3_to_quat_wxyz(_euler_xyz_to_mat3(0, 0, 3.0))
    for t in (0.0, 0.3, 0.5, 1.0):
        q = _slerp_wxyz(qa, qb, t)
        assert sum(v * v for v in q) == pytest.approx(1.0)


def test_slerp_takes_shortest_arc():
    """yaw ±170° 间插值应跨 180° 短弧,而非绕 340° 长弧。"""
    a = math.radians(170)
    qa = _mat3_to_quat_wxyz(_euler_xyz_to_mat3(0, 0, a))
    qb = _mat3_to_quat_wxyz(_euler_xyz_to_mat3(0, 0, -a))
    q_mid = _slerp_wxyz(qa, qb, 0.5)
    m = _quat_wxyz_to_mat3(q_mid)
    # 中点朝向应为 ±180°(cos≈-1),长弧中点会是 0°(cos≈+1)
    forward = _mat_vec(m, (1.0, 0.0, 0.0))
    assert forward[0] == pytest.approx(-1.0, abs=1e-6)
