"""Observe an explicit CUDA ordinal; managed use requires the approved UUID."""

from __future__ import annotations

import ctypes
import uuid


def verify_device(
    device: str, expected_uuid: str | None, *, local_exclusive: bool = False
):
    if device == "cpu":
        if expected_uuid:
            raise ValueError("CPU deployment must not specify a GPU UUID")
        return
    import torch

    selected = torch.device(device)
    if (
        selected.type != "cuda"
        or selected.index is None
        or not torch.cuda.is_available()
    ):
        raise ValueError("A visible, explicit CUDA ordinal is required")
    if torch.version.cuda != "13.2":
        raise ValueError("CUDA build does not match qualification")
    if expected_uuid is None and not local_exclusive:
        raise ValueError("An approved physical GPU UUID is required")
    if expected_uuid is not None and not expected_uuid.startswith("GPU-"):
        raise ValueError("Invalid physical GPU UUID")
    driver = ctypes.CDLL("libcuda.so.1")
    device_id = ctypes.c_int()
    raw = (ctypes.c_ubyte * 16)()
    if (
        driver.cuInit(0) != 0
        or driver.cuDeviceGet(ctypes.byref(device_id), selected.index) != 0
        or driver.cuDeviceGetUuid(ctypes.byref(raw), device_id) != 0
    ):
        raise RuntimeError("Could not establish CUDA physical device identity")
    actual = "GPU-" + str(uuid.UUID(bytes=bytes(raw)))
    if expected_uuid is not None and actual.lower() != expected_uuid.lower():
        raise ValueError("CUDA physical GPU differs from approved binding")
    return actual
