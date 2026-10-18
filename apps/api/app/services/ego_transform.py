"""v0.15.1 · ego pose 几何纯函数(运动补偿 propagate + 区间插值的数学核心)

所有变换在「世界系(ego→global)」里做;框 PSR 是「该帧 ego 系」:

- 运动补偿:world = ego_to_world(psr_i, pose_i) → psr_j = world_to_ego(world, pose_j)。
  静止物 world 不变 → psr_j 自动"追平"ego 运动。
- 区间插值:两端框各转世界系 → 线性插值中心 / slerp 插值朝向 / 线性插值尺寸
  → 投回中间帧 ego 系。
- 无 pose 降级:任一帧缺 pose → 世界变换退化为恒等(等价 v0.14.1 原样复制 /
  纯 ego 系线性插值),返回 motion_compensated=False。

坐标系契约(v0.13.11):DB 内 PSR 永远 ISO 字节;ego pose 是 ego→global
(ISO 8855 世界系),与 axis_convention 正交。euler ↔ mat3 与
axis_convention.py / 前端 three.js Euler XYZ 同约定,复用其实现保持锁步。
"""

from __future__ import annotations

import math

from aap_assets_catalog.spatial_values import FramePose
from aap_assets_catalog.axis_convention import (
    Mat3,
    PsrDict,
    _euler_xyz_to_mat3,
    _mat3_to_euler_xyz,
    _mat_mul,
    _mat_vec,
    _transpose,
)

Vec3 = tuple[float, float, float]


def _quat_wxyz_to_mat3(q: list[float]) -> Mat3:
    """[w,x,y,z] 四元数 → 3x3 旋转矩阵(row-major Mat3)。"""
    w, x, y, z = q
    n = w * w + x * x + y * y + z * z
    if n < 1e-12:
        return (1, 0, 0, 0, 1, 0, 0, 0, 1)
    s = 2.0 / n
    wx, wy, wz = s * w * x, s * w * y, s * w * z
    xx, xy, xz = s * x * x, s * x * y, s * x * z
    yy, yz, zz = s * y * y, s * y * z, s * z * z
    return (
        1.0 - (yy + zz),
        xy - wz,
        xz + wy,
        xy + wz,
        1.0 - (xx + zz),
        yz - wx,
        xz - wy,
        yz + wx,
        1.0 - (xx + yy),
    )


def _mat3_to_quat_wxyz(m: Mat3) -> list[float]:
    """3x3 旋转矩阵 → 单位四元数 [w,x,y,z](Shepperd 法,数值稳定)。"""
    tr = m[0] + m[4] + m[8]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2.0
        return [0.25 * s, (m[7] - m[5]) / s, (m[2] - m[6]) / s, (m[3] - m[1]) / s]
    if m[0] > m[4] and m[0] > m[8]:
        s = math.sqrt(1.0 + m[0] - m[4] - m[8]) * 2.0
        return [(m[7] - m[5]) / s, 0.25 * s, (m[1] + m[3]) / s, (m[2] + m[6]) / s]
    if m[4] > m[8]:
        s = math.sqrt(1.0 + m[4] - m[0] - m[8]) * 2.0
        return [(m[2] - m[6]) / s, (m[1] + m[3]) / s, 0.25 * s, (m[5] + m[7]) / s]
    s = math.sqrt(1.0 + m[8] - m[0] - m[4]) * 2.0
    return [(m[3] - m[1]) / s, (m[2] + m[6]) / s, (m[5] + m[7]) / s, 0.25 * s]


def _slerp_wxyz(qa: list[float], qb: list[float], t: float) -> list[float]:
    """单位四元数球面插值;dot<0 取反走短弧;夹角极小退化为 nlerp。"""
    dot = sum(a * b for a, b in zip(qa, qb))
    qb = list(qb)
    if dot < 0.0:
        dot = -dot
        qb = [-v for v in qb]
    if dot > 1.0 - 1e-9:
        out = [a + (b - a) * t for a, b in zip(qa, qb)]
    else:
        theta = math.acos(max(-1.0, min(1.0, dot)))
        sin_theta = math.sin(theta)
        wa = math.sin((1.0 - t) * theta) / sin_theta
        wb = math.sin(t * theta) / sin_theta
        out = [wa * a + wb * b for a, b in zip(qa, qb)]
    norm = math.sqrt(sum(v * v for v in out))
    return [v / norm for v in out]


def _pose_rt(pose: FramePose) -> tuple[Mat3, Vec3]:
    """FramePose → (R, t),表示 ego→global。"""
    r = _quat_wxyz_to_mat3([float(v) for v in pose.ego_rotation])
    t = (
        float(pose.ego_translation[0]),
        float(pose.ego_translation[1]),
        float(pose.ego_translation[2]),
    )
    return r, t


def _psr_floats(psr: PsrDict) -> PsrDict:
    return {
        "center": [float(v) for v in psr["center"]],
        "size": [float(v) for v in psr["size"]],
        "rotation": [float(v) for v in psr["rotation"]],
    }


def box_ego_to_world(psr: PsrDict, pose: FramePose) -> tuple[Vec3, Mat3]:
    """框(某帧 ego 系 PSR)→ 世界系 (center, rotation 矩阵)。size 不变,不返回。"""
    r_p, t_p = _pose_rt(pose)
    c = _mat_vec(
        r_p, (float(psr["center"][0]), float(psr["center"][1]), float(psr["center"][2]))
    )
    center_w: Vec3 = (c[0] + t_p[0], c[1] + t_p[1], c[2] + t_p[2])
    box_e = _euler_xyz_to_mat3(
        float(psr["rotation"][0]), float(psr["rotation"][1]), float(psr["rotation"][2])
    )
    return center_w, _mat_mul(r_p, box_e)


def box_world_to_ego(
    center_w: Vec3, rot_w: Mat3, size: list[float], pose: FramePose
) -> PsrDict:
    """世界系框 → 某帧 ego 系 PSR(逆变换)。"""
    r_p, t_p = _pose_rt(pose)
    rt = _transpose(r_p)
    d = (center_w[0] - t_p[0], center_w[1] - t_p[1], center_w[2] - t_p[2])
    center_e = _mat_vec(rt, d)
    rot_e = _mat_mul(rt, rot_w)
    return {
        "center": center_e,
        "size": [float(v) for v in size],
        "rotation": _mat3_to_euler_xyz(rot_e),
    }


def compensate_psr(
    psr: PsrDict,
    *,
    pose_src: FramePose | None,
    pose_dst: FramePose | None,
) -> tuple[PsrDict, bool]:
    """运动补偿:源帧 PSR → 世界位置不变 → 目标帧 ego 系 PSR。

    返回 (新 PSR, motion_compensated)。任一帧缺 pose → 恒等复制 + False
    (等价 v0.14.1 原样复制,零回归)。
    """
    if pose_src is None or pose_dst is None:
        return _psr_floats(psr), False
    center_w, rot_w = box_ego_to_world(psr, pose_src)
    return box_world_to_ego(center_w, rot_w, list(psr["size"]), pose_dst), True


def interpolate_psr(
    psr_a: PsrDict,
    psr_b: PsrDict,
    t: float,
    *,
    pose_a: FramePose | None,
    pose_b: FramePose | None,
    pose_mid: FramePose | None,
) -> tuple[PsrDict, bool]:
    """区间插值:帧 a、b 的两框,取参数 t∈[0,1] 处的中间帧框。

    三帧 pose 齐全 → 世界系内线性插值中心 + slerp 朝向 + 线性尺寸,再投回
    中间帧 ego 系(motion_compensated=True);任一缺 → 纯 ego 系插值
    (恒等世界变换,等价"假设车没动",False)。
    """
    size_m = [
        float(sa) + (float(sb) - float(sa)) * t
        for sa, sb in zip(psr_a["size"], psr_b["size"])
    ]

    if pose_a is not None and pose_b is not None and pose_mid is not None:
        ca, ra = box_ego_to_world(psr_a, pose_a)
        cb, rb = box_ego_to_world(psr_b, pose_b)
        center_w: Vec3 = (
            ca[0] + (cb[0] - ca[0]) * t,
            ca[1] + (cb[1] - ca[1]) * t,
            ca[2] + (cb[2] - ca[2]) * t,
        )
        rot_w = _quat_wxyz_to_mat3(
            _slerp_wxyz(_mat3_to_quat_wxyz(ra), _mat3_to_quat_wxyz(rb), t)
        )
        return box_world_to_ego(center_w, rot_w, size_m, pose_mid), True

    # 任一帧缺 pose:三帧必须齐全才能过世界系,混合部分 pose 会把两套
    # 坐标系拼在一起——整体降级为纯 ego 系插值("假设车没动")。
    pa, pb = _psr_floats(psr_a), _psr_floats(psr_b)
    center_e = [
        pa["center"][i] + (pb["center"][i] - pa["center"][i]) * t for i in range(3)
    ]
    rot_e = _quat_wxyz_to_mat3(
        _slerp_wxyz(
            _mat3_to_quat_wxyz(_euler_xyz_to_mat3(*pa["rotation"])),
            _mat3_to_quat_wxyz(_euler_xyz_to_mat3(*pb["rotation"])),
            t,
        )
    )
    return {
        "center": center_e,
        "size": size_m,
        "rotation": _mat3_to_euler_xyz(rot_e),
    }, False
