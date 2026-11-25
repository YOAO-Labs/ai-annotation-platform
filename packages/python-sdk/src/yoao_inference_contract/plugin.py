"""Portable installation intent and fail-closed Registry admission facts.

The manifest is an administrator-approved intent, not evidence that an endpoint
is running the artifact or has qualified for GPU admission.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from uuid import UUID

from yoao_task_contract import Task
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

from .execution import thaw_json

PLUGIN_MANIFEST_OCI_LABEL = "io.yoao.plugin.manifest"
PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL = "io.yoao.plugin.qualification-command"

_MAX_OCI_MANIFEST_BYTES = 65536
_MAX_OCI_QUALIFICATION_COMMAND_BYTES = 4096
_EXECUTABLE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "fish"})

_OCI_DIGEST_REFERENCE = (
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}$"
)


def validate_oci_digest_reference(value: str) -> str:
    """Validate a digest-pinned image reference before passing it to a runtime."""
    if not isinstance(value, str) or re.fullmatch(_OCI_DIGEST_REFERENCE, value) is None:
        raise ValueError("invalid OCI digest-pinned image reference")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _loads_strict_json(raw: object, *, max_bytes: int, label: str) -> Any:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} must be a nonempty JSON string")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    if not raw.strip():
        raise ValueError(f"{label} must be a nonempty JSON string")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_no_duplicate_keys,
            parse_float=_finite_json_float,
            parse_constant=_reject_json_constant,
        )
    except RecursionError as exc:
        raise ValueError(f"{label} JSON nesting depth exceeded") from exc


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
    format: str | None = Field(
        min_length=1,
        description="Null identifies a native checkpoint without a separate export format.",
    )
    runtime_device: Literal["cpu", "cuda"]


class PluginModelCapability(BaseModel):
    """The security-relevant projection of one advertised protocol model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictStr = Field(min_length=1, max_length=128)
    task: Task
    is_interactive: StrictBool
    supported_prompts: list[StrictStr] = Field(min_length=1)
    supported_inputs: list[StrictStr] = Field(min_length=1)
    supported_geometric_outputs: list[StrictStr]

    @model_validator(mode="after")
    def validate_capabilities(self):
        if self.id.strip() != self.id:
            raise ValueError("model id must be canonical")
        for values in (
            self.supported_prompts,
            self.supported_inputs,
            self.supported_geometric_outputs,
        ):
            if any(not value or value.strip() != value for value in values):
                raise ValueError("capabilities must be nonempty canonical strings")
            if len(values) != len(set(values)):
                raise ValueError("capabilities must be unique")
        return self


class PluginPoolTopology(BaseModel):
    """Approved maximum handles for one named pool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cap: int = Field(gt=0, strict=True)


class PluginImageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=128)
    version: str = Field(min_length=1, max_length=128)
    publisher: str = Field(min_length=1, max_length=255)
    license: str = Field(min_length=1, max_length=255)
    sdk_compatibility: str = Field(min_length=1, max_length=255)
    entrypoint: list[str] = Field(min_length=1)
    capabilities: list[Literal["inference", "detection", "managed_models"]] = Field(
        min_length=1
    )
    models: list[PluginModelCapability] = Field(min_length=1, max_length=128)
    pools: dict[str, PluginPoolTopology] = Field(min_length=1, max_length=128)
    resource: PluginResourceRequirements
    asset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_model_compatibility: list[ManagedModelCompatibility] = Field(
        default_factory=list
    )
    allowed_asset_sources: list[str]

    @model_validator(mode="after")
    def validate_intent(self):
        if any(not item or not item.strip() for item in self.entrypoint):
            raise ValueError("entrypoint must contain nonempty arguments")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        if "inference" not in self.capabilities:
            raise ValueError("inference capability is required")
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("model ids must be unique")
        if any(
            not pool or len(pool) > 128 or pool.strip() != pool for pool in self.pools
        ):
            raise ValueError("pool ids must be nonempty and canonical")
        if any(not item or not item.strip() for item in self.allowed_asset_sources):
            raise ValueError("asset sources must not be empty")
        if ("managed_models" in self.capabilities) != bool(
            self.managed_model_compatibility
        ):
            raise ValueError(
                "managed model capability and compatibility declarations must agree"
            )
        if any(
            item.runtime_device != self.resource.device
            for item in self.managed_model_compatibility
        ):
            raise ValueError("managed model runtime must match the instance device")
        return self

    def bind_artifact(self, artifact: str) -> PluginManifest:
        validated = validate_oci_digest_reference(artifact)
        return PluginManifest.model_validate(
            {**self.model_dump(mode="python"), "artifact": validated}
        )


class PluginManifest(PluginImageManifest):
    artifact: str = Field(pattern=_OCI_DIGEST_REFERENCE)


def parse_oci_plugin_manifest(raw: object, *, artifact: str) -> PluginManifest:
    """Parse an OCI manifest label and bind it to the verified digest-pinned artifact."""
    validated_artifact = validate_oci_digest_reference(artifact)
    parsed = _loads_strict_json(
        raw,
        max_bytes=_MAX_OCI_MANIFEST_BYTES,
        label=PLUGIN_MANIFEST_OCI_LABEL,
    )
    if not isinstance(parsed, dict):
        raise ValueError(f"{PLUGIN_MANIFEST_OCI_LABEL} must contain a JSON object")
    return PluginImageManifest.model_validate(parsed).bind_artifact(validated_artifact)


def validate_oci_qualification_command(command: Sequence[object]) -> tuple[str, ...]:
    """Validate a qualification argument vector as a fixed non-shell command."""
    if (
        isinstance(command, (str, bytes, bytearray))
        or not isinstance(command, Sequence)
        or not (1 <= len(command) <= 64)
    ):
        raise ValueError(
            f"{PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL} must be a list of 1 to 64 arguments"
        )
    if any(
        type(arg) is not str
        or not arg
        or arg.strip() != arg
        or any(c in arg for c in "\x00\r\n")
        for arg in command
    ):
        raise ValueError(
            f"{PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL} arguments must be nonempty canonical strings"
        )
    argv = tuple(str(arg) for arg in command)
    executable = argv[0]
    if (
        _EXECUTABLE_NAME.fullmatch(executable) is None
        or executable.lower() in _SHELL_EXECUTABLES
        or (
            executable.lower().startswith("python")
            and any(
                arg == "-c"
                or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])
                for arg in argv[1:]
            )
        )
    ):
        raise ValueError(
            f"{PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL} declares a disallowed executable"
        )
    return argv


def parse_oci_qualification_command(raw: object) -> tuple[str, ...]:
    """Parse an OCI qualification command label as a fixed non-shell argument vector."""
    parsed = _loads_strict_json(
        raw,
        max_bytes=_MAX_OCI_QUALIFICATION_COMMAND_BYTES,
        label=PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL,
    )
    if not isinstance(parsed, list):
        raise ValueError(
            f"{PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL} must be a JSON list of 1 to 64 arguments"
        )
    return validate_oci_qualification_command(parsed)


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
            if (
                not resource.gpu_resource_id
                or not resource.physical_gpu_uuid
                or resource.gpu_resource_id.strip() != resource.gpu_resource_id
                or not resource.physical_gpu_uuid.startswith("GPU-")
                or len(resource.physical_gpu_uuid) <= 4
                or resource.physical_gpu_uuid.strip() != resource.physical_gpu_uuid
                or re.fullmatch(r"cuda:(0|[1-9][0-9]*)", resource.logical_device)
                is None
            ):
                raise ValueError(
                    "CUDA requires an explicit resource, physical UUID, and logical device"
                )
        elif (
            resource.gpu_resource_id is not None
            or resource.physical_gpu_uuid is not None
            or resource.logical_device != "cpu"
        ):
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
    artifact: str = Field(pattern=_OCI_DIGEST_REFERENCE)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource: PluginResourceBinding


class PluginLifecycleQualification(BaseModel):
    """Administrator-approved, canonically hashed lifecycle qualification report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: dict[str, Any]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self):
        try:
            payload = json.dumps(
                self.report,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("lifecycle report must be finite JSON") from exc
        if hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ValueError("lifecycle report digest mismatch")
        return self


class PluginActivateRequest(PluginInstallationCommand):
    receipt: PluginLaunchReceipt
    qualification: PluginLifecycleQualification | None = None


class PluginInstallationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_id: UUID
    installation: PluginInstallation


class PluginQuiesceResponse(PluginInstallationResponse):
    safe_to_stop: bool
    launch: PluginLaunchReceipt


def has_reserved_plugin_metadata(extra_params: object) -> bool:
    """Recognize either installation-owner key, including malformed values."""
    return isinstance(extra_params, Mapping) and (
        "plugin" in extra_params or "plugin_lifecycle" in extra_params
    )


def plugin_is_active(extra_params: object) -> bool:
    """Legacy registries remain usable; malformed reserved metadata fails closed."""
    if extra_params is None:
        return True
    if not isinstance(extra_params, Mapping):
        return False
    if "plugin" not in extra_params:
        return "plugin_lifecycle" not in extra_params
    try:
        return (
            PluginInstallation.model_validate(thaw_json(extra_params["plugin"])).status
            == "active"
        )
    except (ValueError, TypeError):
        return False
