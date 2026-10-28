"""Small, framework-independent model author contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from PIL.Image import Image


@dataclass(frozen=True, slots=True)
class Detection:
    """A box in pixels relative to the image passed to ``predict``."""
    x1: float
    y1: float
    x2: float
    y2: float
    class_index: int
    score: float


class DetectionModel(Protocol):
    classes: Sequence[str]

    def predict(self, image: Image, parameters: Mapping[str, Any]) -> Sequence[Detection]: ...
    def close(self) -> None: ...


class DetectionProvider(Protocol):
    def load(self, model_id: str, device: str, config: Mapping[str, Any]) -> DetectionModel: ...


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    token: str
    device: str
    catalog: Mapping[str, Mapping[str, Any]]
    default_model: str
    versions: Mapping[str, str] = field(default_factory=dict)
    series: str = "detection"
    physical_gpu_uuid: str | None = None
    plugin_identity: Mapping[str, str] = field(default_factory=dict)
    runtime_info: Mapping[str, Any] | Callable[[], Mapping[str, Any]] = field(default_factory=dict)
    managed_store: Any | None = None
    runtime_receipt: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    install_request_model: type | None = None
    managed_model_compatibility: Sequence[Mapping[str, str]] = ()
    verify_keyring: Mapping[str, Any] = field(default_factory=dict)
    expected_registry_id: str | None = None
    expected_gpu_resource_id: str | None = None
    allowed_image_hosts: Sequence[str] = ()
    evictable_verified: bool = False
    pool_capacity: int = 1

    def __post_init__(self) -> None:
        if len(self.token) < 32:
            raise ValueError("token must contain at least 32 characters")
        if self.device != "cpu" and not (self.device.startswith("cuda:") and self.device[5:].isdigit()):
            raise ValueError("device must be cpu or cuda:<ordinal>")
        if self.device == "cpu" and self.physical_gpu_uuid is not None:
            raise ValueError("CPU device cannot have a physical GPU UUID")
        if self.device.startswith("cuda:") and (not self.physical_gpu_uuid or not self.physical_gpu_uuid.startswith("GPU-")):
            raise ValueError("CUDA device requires an observed physical GPU UUID")
        if not self.catalog or self.default_model not in self.catalog:
            raise ValueError("default model must be present in a nonempty catalog")
        if any(not isinstance(k, str) or not k or not isinstance(v, Mapping) for k, v in self.catalog.items()):
            raise ValueError("catalog entries must map nonempty names to configurations")
        if not isinstance(self.series, str) or not self.series:
            raise ValueError("series must be nonempty")
        for entry in self.managed_model_compatibility:
            if set(entry) != {"framework", "format", "runtime_device"} or any(not isinstance(v, str) or not v for v in entry.values()):
                raise ValueError("managed compatibility requires framework, format and runtime_device")
            if entry["runtime_device"] != ("cuda" if self.device.startswith("cuda:") else "cpu"):
                raise ValueError("managed compatibility device differs from configured device")
        if self.managed_model_compatibility and self.managed_store is None:
            raise ValueError("managed compatibility requires managed model support")
        if self.pool_capacity < 1:
            raise ValueError("pool capacity must be positive")
        managed = (self.managed_store is not None, self.runtime_receipt is not None, self.install_request_model is not None)
        if any(managed) and not all(managed):
            raise ValueError("managed store, runtime receipt callback and install request model must be configured together")
        if self.verify_keyring and not (self.expected_registry_id and self.expected_gpu_resource_id):
            raise ValueError("managed GPU admission requires fixed registry and resource identities")
        if self.evictable_verified and not self.verify_keyring:
            raise ValueError("verified lifecycle requires an admission keyring")
