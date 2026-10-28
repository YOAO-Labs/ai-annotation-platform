"""Public plugin manifest and Registry installation types."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        if len(set(self.capabilities)) != len(self.capabilities) or "inference" not in self.capabilities:
            raise ValueError("capabilities must be unique and include inference")
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


class PluginPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1)
    auth_token: str = Field(min_length=32, repr=False)
    manifest: PluginManifest
    resource: PluginResourceBinding

    @model_validator(mode="after")
    def validate_resource(self):
        PluginInstallation(installation_id=self.installation_id, manifest=self.manifest,
                           status="prepared", revision=1, resource=self.resource)
        return self


class PluginRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class PluginLaunchReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: UUID
    registry_id: UUID
    artifact: str = Field(pattern=r"^\S+@sha256:[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource: PluginResourceBinding


class PluginActivateRequest(PluginRevisionRequest):
    receipt: PluginLaunchReceipt


class PluginInstallationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registry_id: UUID
    installation: PluginInstallation


class PluginQuiesceResult(PluginInstallationResult):
    safe_to_stop: bool = Field(strict=True)
