"""Bounded binary PCD and row-aligned audit serialization."""

import io
import re

import numpy as np

from .sources import AuxiliaryError

MAX_CAMERA_POINTS = 500_000
MAX_DERIVED_BYTES = 32 * 1024 * 1024


def encode_cloud(cloud: dict) -> tuple[bytes, bytes, int]:
    xyz = np.asarray(cloud["xyz"])
    rgb = np.asarray(cloud["rgb"])
    if (
        xyz.ndim != 2
        or xyz.shape[1] != 3
        or len(xyz) > MAX_CAMERA_POINTS
        or rgb.shape != xyz.shape
    ):
        raise AuxiliaryError("cloud_shape_invalid")
    if (
        not np.isfinite(xyz).all()
        or not np.isfinite(rgb).all()
        or np.any(np.abs(xyz) > 100_000)
        or rgb.dtype != np.uint8
    ):
        raise AuxiliaryError("cloud_values_invalid")
    rows = np.empty(
        len(xyz), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<u4")]
    )
    rows["x"], rows["y"], rows["z"] = xyz.T
    colors = rgb.astype(np.uint32)
    rows["rgb"] = (colors[:, 0] << 16) | (colors[:, 1] << 8) | colors[:, 2]
    header = (
        "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z rgb\nSIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\n"
        + f"WIDTH {len(rows)}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {len(rows)}\nDATA binary\n"
    ).encode("ascii")
    audit = {}
    for name, value in cloud.items():
        if name in {"xyz", "rgb"}:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", name):
            raise AuxiliaryError("audit_field_invalid")
        array = np.asarray(value)
        if (
            array.ndim not in {1, 2}
            or len(array) != len(rows)
            or array.dtype.kind not in "buif"
            or array.nbytes > MAX_DERIVED_BYTES
        ):
            raise AuxiliaryError("audit_shape_invalid")
        if not np.isfinite(array).all():
            raise AuxiliaryError("audit_values_invalid")
        audit[name] = array
    audit_bytes = io.BytesIO()
    # The empty archive still declares that no extra source-pixel evidence was
    # provided; it never invents confidence values for an imported experiment.
    np.savez_compressed(audit_bytes, **audit)
    output = header + rows.tobytes()
    sidecar = audit_bytes.getvalue()
    if len(output) + len(sidecar) > MAX_DERIVED_BYTES:
        raise AuxiliaryError("derived_bytes_exceeded")
    return output, sidecar, len(rows)
