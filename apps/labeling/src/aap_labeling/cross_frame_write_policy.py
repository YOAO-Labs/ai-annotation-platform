"""Portable geometry and identity policy for cross-frame annotation writes."""

from __future__ import annotations

import math
from typing import Protocol, TypedDict


PROPAGATABLE_GEOMETRY_TYPES = frozenset(
    {
        "box_3d",
        "bbox",
        "polygon",
        "multi_polygon",
        "rotated_bbox",
        "polyline",
        "keypoint",
    }
)

Mat3 = tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
]
Vec3 = tuple[float, float, float]


class PsrDict(TypedDict):
    center: list[float]
    size: list[float]
    rotation: list[float]


class FramePose(Protocol):
    ego_rotation: list[float]
    ego_translation: list[float]


def _mat_vec(m: Mat3, v: Vec3) -> list[float]:
    return [
        m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
        m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
        m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
    ]


def _transpose(m: Mat3) -> Mat3:
    return (m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8])


def _mat_mul(a: Mat3, b: Mat3) -> Mat3:
    return (
        a[0] * b[0] + a[1] * b[3] + a[2] * b[6],
        a[0] * b[1] + a[1] * b[4] + a[2] * b[7],
        a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
        a[3] * b[0] + a[4] * b[3] + a[5] * b[6],
        a[3] * b[1] + a[4] * b[4] + a[5] * b[7],
        a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
        a[6] * b[0] + a[7] * b[3] + a[8] * b[6],
        a[6] * b[1] + a[7] * b[4] + a[8] * b[7],
        a[6] * b[2] + a[7] * b[5] + a[8] * b[8],
    )


def _euler_xyz_to_mat3(rx: float, ry: float, rz: float) -> Mat3:
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return (
        cy * cz,
        -cy * sz,
        sy,
        cx * sz + cz * sx * sy,
        cx * cz - sx * sy * sz,
        -cy * sx,
        sx * sz - cx * cz * sy,
        cz * sx + cx * sy * sz,
        cx * cy,
    )


def _mat3_to_euler_xyz(m: Mat3) -> list[float]:
    sy = max(-1.0, min(1.0, m[2]))
    ry = math.asin(sy)
    cy = math.cos(ry)
    if abs(cy) > 1e-6:
        rx = math.atan2(-m[5], m[8])
        rz = math.atan2(-m[1], m[0])
    else:
        rx = math.atan2(m[7], m[4])
        rz = 0.0
    return [rx, ry, rz]


def _quat_wxyz_to_mat3(q: list[float]) -> Mat3:
    """[w,x,y,z] quaternion to a row-major 3x3 rotation matrix."""
    w, x, y, z = q
    n = w * w + x * x + y * y + z * z
    if n < 1e-12:
        return (1, 0, 0, 0, 1, 0, 0, 0, 1)
    s = 2.0 / n
    wx, wy, wz = s * w * x, s * w * y, s * w * z
    xx, xz, yy = s * x * x, s * x * z, s * y * y
    yz, zz = s * y * z, s * z * z
    return (
        1.0 - (yy + zz),
        s * (x * y) - wz,
        xz + wy,
        s * (x * y) + wz,
        1.0 - (xx + zz),
        yz - wx,
        xz - wy,
        yz + wx,
        1.0 - (xx + yy),
    )


def _mat3_to_quat_wxyz(m: Mat3) -> list[float]:
    """3x3 rotation matrix to a stable unit quaternion [w,x,y,z]."""
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
    """Spherical interpolation on the shortest quaternion arc."""
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
    """Frame pose to (R, t), where R and t map ego to global."""
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
    """Convert one ego-frame PSR to world center and rotation."""
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
    """Convert a world-frame box to one ego-frame PSR."""
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
    """Keep world position fixed while moving a box between ego frames."""
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
    """Interpolate center/size linearly and orientation with quaternion slerp."""
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


__all__ = [
    "PROPAGATABLE_GEOMETRY_TYPES",
    "PsrDict",
    "Vec3",
    "box_ego_to_world",
    "box_world_to_ego",
    "compensate_psr",
    "interpolate_psr",
]
