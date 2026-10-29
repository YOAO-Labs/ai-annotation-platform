"""free_gpu_memory / gpu_info_snapshot: 无 GPU (CI 常态) 时不抛错、返回 {}。"""

from __future__ import annotations

import ctypes
import sys
import types

import pytest

from aap_backend_runtime import (
    free_gpu_memory,
    gpu_info_snapshot,
    physical_gpu_identity,
    validate_single_gpu_device_set,
)
from aap_backend_runtime.gpu import (
    observed_cuda_runtime_version,
    observed_single_nvidia_gpu,
)


def test_free_gpu_memory_no_raise() -> None:
    # 无 torch / 无 CUDA 时应是 no-op, 不抛异常。
    free_gpu_memory()


def test_gpu_info_snapshot_shape() -> None:
    snap = gpu_info_snapshot()
    assert isinstance(snap, dict)
    # 无 GPU 时空字典; 有 GPU 时含显存键。
    if snap:
        assert "memory_total_mb" in snap
        assert "memory_free_mb" in snap
        assert "memory_used_mb" in snap


def test_free_gpu_memory_ignores_broken_cuda_cleanup(monkeypatch) -> None:
    mock_torch = types.ModuleType("torch")
    mock_cuda = types.ModuleType("torch.cuda")
    mock_cuda.is_available = lambda: True
    mock_cuda.empty_cache = lambda: (_ for _ in ()).throw(RuntimeError("CUDA error"))
    mock_cuda.ipc_collect = lambda: (_ for _ in ()).throw(RuntimeError("CUDA error"))
    mock_torch.cuda = mock_cuda
    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    monkeypatch.setitem(sys.modules, "torch.cuda", mock_cuda)

    free_gpu_memory()


def test_free_gpu_memory_ignores_broken_availability_check(monkeypatch) -> None:
    mock_torch = types.ModuleType("torch")
    mock_cuda = types.ModuleType("torch.cuda")
    mock_cuda.is_available = lambda: (_ for _ in ()).throw(
        RuntimeError("driver unavailable")
    )
    mock_torch.cuda = mock_cuda
    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    monkeypatch.setitem(sys.modules, "torch.cuda", mock_cuda)

    free_gpu_memory()


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"NVIDIA_VISIBLE_DEVICES": "0"},
        {"NVIDIA_VISIBLE_DEVICES": "GPU-abc"},
        {"NVIDIA_VISIBLE_DEVICES": "MIG-GPU-abc/1/0"},
        {"CUDA_VISIBLE_DEVICES": "0"},
        {"NVIDIA_VISIBLE_DEVICES": "none"},
        {"CUDA_VISIBLE_DEVICES": "-1"},
    ],
)
def test_single_gpu_device_set_accepts_zero_or_one_device(
    environ: dict[str, str],
) -> None:
    validate_single_gpu_device_set(environ)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NVIDIA_VISIBLE_DEVICES", "0,1"),
        ("NVIDIA_VISIBLE_DEVICES", "GPU-a,GPU-b"),
        ("CUDA_VISIBLE_DEVICES", "0, 1"),
        ("HIP_VISIBLE_DEVICES", "0,1"),
        ("ROCR_VISIBLE_DEVICES", "0,1"),
        ("GPU_DEVICE_ORDINAL", "0,1"),
        ("NVIDIA_VISIBLE_DEVICES", " 0"),
    ],
)
def test_single_gpu_device_set_rejects_multi_or_unbounded_visibility(
    name: str,
    value: str,
) -> None:
    with pytest.raises(RuntimeError, match="multi-device sets are unsupported"):
        validate_single_gpu_device_set({name: value})


@pytest.mark.parametrize("value", ["all", "nvidia.com/gpu=all"])
def test_single_gpu_device_set_allows_cuda_image_default_without_runtime(
    monkeypatch, value: str
) -> None:
    monkeypatch.setattr(
        "aap_backend_runtime.gpu._gpu_runtime_device_present",
        lambda: False,
    )

    validate_single_gpu_device_set({"NVIDIA_VISIBLE_DEVICES": value})


@pytest.mark.parametrize("value", ["all", "nvidia.com/gpu=all"])
def test_single_gpu_device_set_rejects_all_with_gpu_runtime(
    monkeypatch, value: str
) -> None:
    monkeypatch.setattr(
        "aap_backend_runtime.gpu._gpu_runtime_device_present",
        lambda: True,
    )

    with pytest.raises(RuntimeError, match="multi-device sets are unsupported"):
        validate_single_gpu_device_set({"NVIDIA_VISIBLE_DEVICES": value})


def test_single_gpu_device_set_checks_every_runtime_visibility_layer() -> None:
    with pytest.raises(RuntimeError, match="CUDA_VISIBLE_DEVICES"):
        validate_single_gpu_device_set(
            {
                "NVIDIA_VISIBLE_DEVICES": "0",
                "CUDA_VISIBLE_DEVICES": "0,1",
            }
        )


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {"NVIDIA_VISIBLE_DEVICES": "1", "CUDA_VISIBLE_DEVICES": "0"},
            {"physical_device_token": "index:1", "device_index": 1},
        ),
        (
            {"NVIDIA_VISIBLE_DEVICES": "GPU-abc", "CUDA_VISIBLE_DEVICES": "0"},
            {"physical_device_token": "GPU-abc", "device_uuid": "GPU-abc"},
        ),
        (
            {"NVIDIA_VISIBLE_DEVICES": "MIG-GPU-abc/1/0"},
            {
                "physical_device_token": "MIG-GPU-abc/1/0",
                "mig_uuid": "MIG-GPU-abc/1/0",
            },
        ),
        (
            {
                "AAP_GPU_PHYSICAL_DEVICE_TOKEN": "index:1",
                "NVIDIA_VISIBLE_DEVICES": "void",
                "CUDA_VISIBLE_DEVICES": "0",
            },
            {"physical_device_token": "index:1", "device_index": 1},
        ),
        ({"NVIDIA_VISIBLE_DEVICES": "none"}, {}),
    ],
)
def test_physical_gpu_identity_prefers_container_runtime_selection(
    environ: dict[str, str], expected: dict[str, str | int]
) -> None:
    assert physical_gpu_identity(environ) == expected


@pytest.mark.parametrize("value", ["", " all", "all", "0,1", "index:x"])
def test_explicit_physical_gpu_token_is_strict(value: str) -> None:
    with pytest.raises(RuntimeError, match="AAP_GPU_PHYSICAL_DEVICE_TOKEN"):
        physical_gpu_identity({"AAP_GPU_PHYSICAL_DEVICE_TOKEN": value})


def test_observed_gpu_requires_exact_single_physical_identity(monkeypatch) -> None:
    uuid = "GPU-0d0ff398-3fa6-778c-8611-a45fbadecabb"
    monkeypatch.setattr(
        "aap_backend_runtime.gpu.subprocess.check_output",
        lambda *args, **kwargs: f"{uuid}, 24576, 560.35\n",
    )
    assert observed_single_nvidia_gpu(uuid) == {
        "device_uuid": uuid,
        "total_memory_mb": 24576,
        "driver_version": "560.35",
    }
    with pytest.raises(RuntimeError, match="do not match"):
        observed_single_nvidia_gpu("GPU-ac5e7f45-7a36-c3f8-1f65-a54f28bfcd31")


@pytest.mark.parametrize(
    "output",
    [
        "",
        "GPU-0d0ff398-3fa6-778c-8611-a45fbadecabb, 0, 560.35",
        "GPU-0d0ff398-3fa6-778c-8611-a45fbadecabb, N/A, 560.35",
        "GPU-0d0ff398-3fa6-778c-8611-a45fbadecabb, 24576, ",
        "GPU-0d0ff398-3fa6-778c-8611-a45fbadecabb, 24576, 560.35\nGPU-other, 24576, 560.35",
    ],
)
def test_observed_gpu_rejects_missing_or_ambiguous_facts(monkeypatch, output) -> None:
    monkeypatch.setattr(
        "aap_backend_runtime.gpu.subprocess.check_output",
        lambda *args, **kwargs: output,
    )
    with pytest.raises(RuntimeError):
        observed_single_nvidia_gpu("GPU-0d0ff398-3fa6-778c-8611-a45fbadecabb")


@pytest.mark.parametrize(
    ("status", "version", "expected"),
    [(0, 12080, "12.8.0"), (0, 12090, "12.9.0"), (1, 12080, None), (0, 0, None)],
)
def test_observed_cuda_runtime_uses_library_result(
    monkeypatch, status, version, expected
):
    def get_version(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))[0] = version
        return status

    monkeypatch.setattr(
        "aap_backend_runtime.gpu.ctypes.CDLL",
        lambda name: types.SimpleNamespace(cudaRuntimeGetVersion=get_version),
    )
    if expected is None:
        with pytest.raises(RuntimeError, match="did not report"):
            observed_cuda_runtime_version()
    else:
        assert observed_cuda_runtime_version() == expected


def test_observed_cuda_runtime_requires_library(monkeypatch):
    def missing(_name):
        raise OSError("not installed")

    monkeypatch.setattr("aap_backend_runtime.gpu.ctypes.CDLL", missing)
    with pytest.raises(RuntimeError, match="Cannot inspect"):
        observed_cuda_runtime_version()
