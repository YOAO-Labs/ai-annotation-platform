"""Portable installation intent and fail-closed Registry admission facts.

The manifest is an administrator-approved intent, not evidence that an endpoint
is running the artifact or has qualified for GPU admission.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .execution import thaw_json


class PluginResourceRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: Literal["cpu", "cuda"]
    memory_mb: int = Field(gt=0, strict=True)
    gpu_memory_mb: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_device(self):
        if (self.device == "cuda") != (self.gpu_memory_mb > 0):
            raise ValueError("CUDA requires a GPU memory budget; CPU must not have one")
        return self


class ManagedModelCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    framework: str = Field(min_length=1)
    format: str = Field(min_length=1)
    runtime_device: Literal["cpu", "cuda"]


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=128)
    version: str = Field(min_length=1, max_length=128)
    publisher: str = Field(min_length=1, max_length=255)
    license: str = Field(min_length=1, max_length=255)
    artifact: str = Field(pattern=r"^\S+@sha256:[0-9a-f]{64}$")
    sdk_compatibility: str = Field(min_length=1, max_length=255)
    entrypoint: list[str] = Field(min_length=1)
    capabilities: list[Literal["inference", "detection", "managed_models"]] = Field(min_length=1)
    resource: PluginResourceRequirements
    asset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_model_compatibility: list[ManagedModelCompatibility] = Field(default_factory=list)
    allowed_asset_sources: list[str]

    @model_validator(mode="after")
    def validate_intent(self):
        if any(not item or not item.strip() for item in self.entrypoint):
            raise ValueError("entrypoint must contain nonempty arguments")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        if "inference" not in self.capabilities:
            raise ValueError("inference capability is required")
        if any(not item or not item.strip() for item in self.allowed_asset_sources):
            raise ValueError("asset sources must not be empty")
        if ("managed_models" in self.capabilities) != bool(self.managed_model_compatibility):
            raise ValueError("managed model capability and compatibility declarations must agree")
        if any(item.runtime_device != self.resource.device for item in self.managed_model_compatibility):
            raise ValueError("managed model runtime must match the instance device")
        return self

class PluginResourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gpu_resource_id: str | None = None
    physical_gpu_uuid: str | None = None
    logical_device: str


class PluginInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: UUID
    manifest: PluginManifest
    status: Literal["prepared", "active", "disabled", "retired"]
    revision: int = Field(ge=1, strict=True)
    resource: PluginResourceBinding

    @model_validator(mode="after")
    def validate_binding(self):
        resource = self.resource
        if self.manifest.resource.device == "cuda":
            if not resource.gpu_resource_id or not resource.physical_gpu_uuid or not resource.logical_device.startswith("cuda:"):
                raise ValueError("CUDA requires an explicit resource, physical UUID, and logical device")
        elif resource.gpu_resource_id is not None or resource.physical_gpu_uuid is not None or resource.logical_device != "cpu":
            raise ValueError("CPU installation must not have a GPU binding")
        return self


class PreparePluginInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1)
    auth_token: str = Field(min_length=32, repr=False)
    manifest: PluginManifest
    resource: PluginResourceBinding

    @model_validator(mode="after")
    def validate_resource(self):
        PluginInstallation(
            installation_id=self.installation_id,
            manifest=self.manifest,
            status="prepared",
            revision=1,
            resource=self.resource,
        )
        return self


class PluginInstallationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)


class PluginLaunchReceipt(BaseModel):
    """Trusted administrator's local container-runtime inspection evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: UUID
    registry_id: UUID
    artifact: str = Field(pattern=r"^\S+@sha256:[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource: PluginResourceBinding


class PluginActivateRequest(PluginInstallationCommand):
    receipt: PluginLaunchReceipt


class PluginInstallationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_id: UUID
    installation: PluginInstallation


class PluginQuiesceResponse(PluginInstallationResponse):
    safe_to_stop: bool


def plugin_is_active(extra_params: object) -> bool:
    """Legacy registries remain usable; malformed reserved metadata fails closed."""
    if extra_params is None:
        return True
    if not isinstance(extra_params, Mapping):
        return False
    if "plugin" not in extra_params:
        return True
    try:
        return PluginInstallation.model_validate(thaw_json(extra_params["plugin"])).status == "active"
    except (ValueError, TypeError):
        return False
