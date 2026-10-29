"""ML backend 运行时共享无状态叶子函数。"""

from aap_backend_runtime.config import deployment_verified_flag
from aap_backend_runtime.device import (
    DeviceUnavailableError,
    effective_device,
    effective_device_value,
    is_device_error,
    latch_cpu,
    require_gpu_device,
)
from aap_backend_runtime.gpu import (
    free_gpu_memory,
    gpu_info_snapshot,
    physical_gpu_identity,
    validate_single_gpu_device_set,
)
from aap_backend_runtime.image import fetch_image
from aap_backend_runtime.lifecycle_evidence import (
    artifact_evidence,
    build_managed_lifecycle_evidence,
)
from aap_backend_runtime.lifecycle_validation import exercise_lifecycle_fault_matrix
from aap_backend_runtime.managed_pool import (
    BuildArtifact,
    ManagedBuildTimeout,
    ManagedLruPool,
    ManagedPoolBusyError,
    ResourceLease,
)
from aap_backend_runtime.versions import versions_payload
from aap_backend_runtime.tracker_sessions import (
    TrackerSessionLost,
    TrackerSessionManager,
)

__all__ = [
    "BuildArtifact",
    "DeviceUnavailableError",
    "ManagedBuildTimeout",
    "ManagedLruPool",
    "ManagedPoolBusyError",
    "ResourceLease",
    "artifact_evidence",
    "build_managed_lifecycle_evidence",
    "deployment_verified_flag",
    "effective_device",
    "effective_device_value",
    "exercise_lifecycle_fault_matrix",
    "fetch_image",
    "free_gpu_memory",
    "gpu_info_snapshot",
    "is_device_error",
    "latch_cpu",
    "physical_gpu_identity",
    "require_gpu_device",
    "validate_single_gpu_device_set",
    "versions_payload",
    "TrackerSessionLost",
    "TrackerSessionManager",
]
