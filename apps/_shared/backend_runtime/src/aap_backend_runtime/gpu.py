"""CUDA 显存释放 / 快照 (pytorch backend 共性叶子函数)。

torch 故意惰性 import 在 try 内: onnx / 纯 CPU 环境无 torch 时降级为 no-op / 返回 {}。
"""

from __future__ import annotations

import ctypes
import gc
import os
import re
import subprocess
from collections.abc import Mapping
from typing import Any


_VISIBLE_DEVICE_ENV_NAMES = (
    "NVIDIA_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "GPU_DEVICE_ORDINAL",
)
_NO_DEVICE_VALUES = {"", "-1", "none", "void"}
_NVIDIA_ALL_DEVICE_VALUES = {"all", "nvidia.com/gpu=all"}
_PHYSICAL_DEVICE_TOKEN_ENV_NAME = "AAP_GPU_PHYSICAL_DEVICE_TOKEN"


def _physical_identity_from_token(
    raw: str,
    *,
    name: str,
) -> dict[str, str | int]:
    token = raw.strip()
    if raw != token or not token or "," in token:
        raise RuntimeError(f"{name} must select exactly one physical GPU/MIG token")
    lowered = token.lower()
    if lowered in _NO_DEVICE_VALUES or lowered in _NVIDIA_ALL_DEVICE_VALUES:
        raise RuntimeError(f"{name} must select exactly one physical GPU/MIG token")
    if token.isdigit():
        device_index = int(token)
        return {
            "physical_device_token": f"index:{device_index}",
            "device_index": device_index,
        }
    if token.startswith("index:"):
        raw_index = token.removeprefix("index:")
        if not raw_index.isdigit():
            raise RuntimeError(f"{name} must select exactly one physical GPU/MIG token")
        device_index = int(raw_index)
        return {
            "physical_device_token": token,
            "device_index": device_index,
        }
    if token.startswith("GPU-"):
        return {"physical_device_token": token, "device_uuid": token}
    if token.startswith("MIG-"):
        return {"physical_device_token": token, "mig_uuid": token}
    raise RuntimeError(f"{name} must select exactly one physical GPU/MIG token")


def _gpu_runtime_device_present() -> bool:
    """Return whether a container runtime exposed an NVIDIA or ROCm device."""

    return os.path.exists("/dev/nvidiactl") or os.path.exists("/dev/kfd")


def validate_single_gpu_device_set(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Reject an explicit GPU visibility set containing more than one device.

    Managed lifecycle state is scoped to exactly one physical GPU/MIG resource.
    An unset or explicit no-device value remains valid for CPU deployments.
    Base CUDA images commonly default to ``all`` even without a GPU runtime, so
    that value is tolerated only while no GPU device is actually exposed.
    """

    values = os.environ if environ is None else environ
    for name in _VISIBLE_DEVICE_ENV_NAMES:
        raw = values.get(name)
        if raw is None:
            continue
        value = raw.strip()
        lowered = value.lower()
        if lowered in _NO_DEVICE_VALUES:
            continue
        all_device_values = (
            _NVIDIA_ALL_DEVICE_VALUES if name == "NVIDIA_VISIBLE_DEVICES" else {"all"}
        )
        unbounded_gpu_set = (
            lowered in all_device_values and _gpu_runtime_device_present()
        )
        if raw != value or unbounded_gpu_set or len(value.split(",")) != 1:
            raise RuntimeError(
                f"{name} must select at most one GPU/MIG device; "
                "multi-device sets are unsupported"
            )
    explicit_physical_token = values.get(_PHYSICAL_DEVICE_TOKEN_ENV_NAME)
    if explicit_physical_token is not None:
        _physical_identity_from_token(
            explicit_physical_token,
            name=_PHYSICAL_DEVICE_TOKEN_ENV_NAME,
        )


def physical_gpu_identity(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    """Return the host-visible physical GPU token selected for this process.

    The Compose-derived explicit token is preferred because container runtimes
    may rewrite ``NVIDIA_VISIBLE_DEVICES`` and remap the selected physical GPU
    to logical CUDA device 0. The result deliberately omits a logical index when
    the runtime selected a UUID.
    """

    values = os.environ if environ is None else environ
    validate_single_gpu_device_set(values)
    explicit_physical_token = values.get(_PHYSICAL_DEVICE_TOKEN_ENV_NAME)
    if explicit_physical_token is not None:
        return _physical_identity_from_token(
            explicit_physical_token,
            name=_PHYSICAL_DEVICE_TOKEN_ENV_NAME,
        )
    for name in ("NVIDIA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
        raw = values.get(name)
        if raw is None:
            continue
        token = raw.strip()
        lowered = token.lower()
        if lowered in _NO_DEVICE_VALUES or lowered in _NVIDIA_ALL_DEVICE_VALUES:
            continue
        return _physical_identity_from_token(token, name=name)
    return {}


def observed_single_nvidia_gpu(expected_uuid: str) -> dict[str, str | int]:
    """Read and bind actual visible hardware facts without a Torch dependency."""
    if (
        re.fullmatch(
            r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            expected_uuid,
        )
        is None
    ):
        raise RuntimeError("A physical NVIDIA GPU UUID is required")
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=uuid,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Cannot inspect the allocated NVIDIA GPU") from exc
    rows = [line.split(",") for line in output.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 3:
        raise RuntimeError("Exactly one inspectable NVIDIA GPU is required")
    uuid, memory, driver = (value.strip() for value in rows[0])
    if (
        uuid != expected_uuid
        or not driver
        or not memory.isdecimal()
        or int(memory) <= 0
    ):
        raise RuntimeError("Observed NVIDIA GPU facts do not match the allocation")
    return {
        "device_uuid": uuid,
        "total_memory_mb": int(memory),
        "driver_version": driver,
    }


def observed_cuda_runtime_version() -> str:
    """Read the CUDA runtime library version actually loaded by this process."""
    try:
        library = ctypes.CDLL("libcudart.so.12")
        get_version = library.cudaRuntimeGetVersion
    except (OSError, AttributeError) as exc:
        raise RuntimeError("Cannot inspect the CUDA runtime library") from exc
    get_version.argtypes = [ctypes.POINTER(ctypes.c_int)]
    get_version.restype = ctypes.c_int
    version = ctypes.c_int()
    if get_version(ctypes.byref(version)) != 0 or version.value <= 0:
        raise RuntimeError("CUDA runtime library did not report its version")
    value = version.value
    return f"{value // 1000}.{(value % 1000) // 10}.{value % 10}"


def free_gpu_memory() -> None:
    """显式释放 CUDA caching allocator 持有的显存, 让 nvidia-smi 立刻可见下降。

    以 sam3 / grounded-sam2 的完整版为准 (gc.collect + empty_cache + ipc_collect 容错);
    torch 不可用时仅跑 gc.collect。
    """
    gc.collect()
    try:
        import torch  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        available = torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        return
    if available:
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        try:
            torch.cuda.ipc_collect()
        except Exception:  # noqa: BLE001
            pass


def gpu_info_snapshot() -> dict[str, Any]:
    """torch CUDA context 视角的显存快照 (used/total/free MB 等)。

    无 torch / 无 GPU / 读取失败均返回 ``{}``。注意这是当前 CUDA 上下文的 free/total
    (多进程共享同卡会系统性低报已用显存); 各 backend 的 /health 若需整卡全局视角 (pynvml)
    或叠加 util/温度/功耗, 仍由其 observability 自行覆盖, 不消费本快照。
    """
    try:
        import torch  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    if not torch.cuda.is_available():
        return {}
    try:
        free_b, total_b = torch.cuda.mem_get_info()
        total_mb = int(total_b / 1024**2)
        free_mb = int(free_b / 1024**2)
        identity = physical_gpu_identity()
        snapshot: dict[str, Any] = {
            "device_name": torch.cuda.get_device_name(0),
            "memory_used_mb": total_mb - free_mb,
            "memory_total_mb": total_mb,
            "memory_free_mb": free_mb,
            "process_memory_mb": int(torch.cuda.memory_reserved() / 1024**2),
        }
        if identity:
            snapshot.update(identity)
        else:
            snapshot["device_index"] = torch.cuda.current_device()
        return snapshot
    except Exception:  # noqa: BLE001
        return {}
