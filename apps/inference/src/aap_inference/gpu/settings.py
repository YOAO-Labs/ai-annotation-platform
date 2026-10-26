"""Framework-free configuration contract for GPU arbitration authority."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from aap_inference.gpu.resource import validate_physical_device_token


class GPUArbiterMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


_MODE_ORDER = {
    GPUArbiterMode.OFF: 0,
    GPUArbiterMode.OBSERVE: 1,
    GPUArbiterMode.ENFORCE: 2,
}


@dataclass(frozen=True)
class GPUResourceSettings:
    """One validated physical GPU/MIG resource supplied by the application edge."""

    resource_id: str
    node_id: str
    physical_device_token: str
    allocatable_mb: int
    mode: GPUArbiterMode | None = None


class _GPUResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(strict=True, min_length=1, max_length=255)
    physical_device_token: str = Field(strict=True, min_length=1, max_length=255)
    allocatable_mb: int = Field(strict=True, gt=0, le=2_147_483_647)
    mode: GPUArbiterMode | None = None

    @field_validator("node_id", "physical_device_token")
    @classmethod
    def _validate_identity_component(cls, value: str, info) -> str:
        if value != value.strip() or any(char.isspace() for char in value):
            raise ValueError(f"{info.field_name} 不得包含空白")
        if "," in value:
            raise ValueError(f"{info.field_name} 不得包含逗号")
        if info.field_name == "node_id" and "/" in value:
            raise ValueError("node_id 不得包含 /")
        if info.field_name == "physical_device_token":
            validate_physical_device_token(value)
        return value

    @property
    def resource_id(self) -> str:
        return f"{self.node_id}/{self.physical_device_token}"


def _json_without_duplicate_keys(raw: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"JSON 存在重复 key: {key}")
            value[key] = item
        return value

    return json.loads(raw, object_pairs_hook=object_pairs)


def parse_gpu_resources(
    raw_json: str,
) -> tuple[dict[str, GPUResourceSettings], tuple[str, ...]]:
    """Parse and validate the shared GPU resource JSON at application edges."""

    if type(raw_json) is not str:
        return {}, ("GPU_ARBITER_RESOURCES_JSON 必须是字符串",)
    raw = raw_json.strip()
    if not raw:
        return {}, ()
    try:
        decoded = _json_without_duplicate_keys(raw)
        if not isinstance(decoded, dict):
            raise ValueError("GPU_ARBITER_RESOURCES_JSON 必须是 JSON object")
        parsed = TypeAdapter(dict[str, _GPUResourceInput]).validate_python(decoded)
        resources: dict[str, GPUResourceSettings] = {}
        for resource_id, resource in parsed.items():
            if resource_id != resource.resource_id:
                raise ValueError(
                    "GPU resource key 必须精确等于 "
                    f"node_id/physical_device_token: {resource.resource_id}"
                )
            resources[resource_id] = GPUResourceSettings(
                resource_id=resource_id,
                node_id=resource.node_id,
                physical_device_token=resource.physical_device_token,
                allocatable_mb=resource.allocatable_mb,
                mode=resource.mode,
            )
        return resources, ()
    except (TypeError, ValueError) as exc:
        return {}, (str(exc),)


@dataclass(frozen=True)
class GPUAuthoritySettings:
    """Only the settings read by GPU authority; secret files stay runtime-owned."""

    gpu_arbiter_mode: GPUArbiterMode
    gpu_arbiter_resources: Mapping[str, GPUResourceSettings] = field(
        default_factory=dict
    )
    gpu_arbiter_config_errors: tuple[str, ...] = ()
    gpu_arbiter_rollout_enabled: bool = False
    gpu_arbiter_admission_timeout_seconds: float = 30
    gpu_arbiter_residency_cooldown_seconds: float = 30
    ml_predict_timeout: int = 100
    redis_url: str = ""
    gpu_lifecycle_signing_keys_file: str = ""
    gpu_lifecycle_active_signing_kid: str = ""
    gpu_arbiter_collector_database_url_file: str = ""

    def gpu_arbiter_desired_mode(self, resource_id: str) -> GPUArbiterMode:
        resource = self.gpu_arbiter_resources.get(resource_id)
        if resource is None:
            return GPUArbiterMode.OFF
        resource_mode = resource.mode or GPUArbiterMode.OFF
        return min(
            (self.gpu_arbiter_mode, resource_mode),
            key=_MODE_ORDER.__getitem__,
        )
