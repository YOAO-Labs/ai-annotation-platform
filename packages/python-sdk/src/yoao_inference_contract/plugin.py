"""Portable installation intent and fail-closed Registry admission facts.

The manifest is an administrator-approved intent, not evidence that an endpoint
is running the artifact or has qualified for GPU admission.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from yoao_task_contract import Task
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

from .execution import thaw_json

PLUGIN_MANIFEST_OCI_LABEL = "io.yoao.plugin.manifest"
PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL = "io.yoao.plugin.qualification-command"

EXTENSION_MANIFEST_SCHEMA = "yoao-extension-manifest/v1"
INFERENCE_PROTOCOL_VERSION = "yoao-inference/v2"
INFERENCE_WIRE_PROTOCOL_VERSION = "2.0"
INFERENCE_EXECUTION_MODES = ("container", "direct")
PLUGIN_SOURCE_TIERS = ("official", "third_party")
INFERENCE_TASK_VALUES = (
    "detect",
    "segment",
    "classify",
    "pose",
    "obb",
    "ocr",
    "doc_layout",
    "tracker",
    "interactive_seg",
)
INFERENCE_CAPABILITIES = (
    "inference",
    "detection",
    "segmentation",
    "interactive",
    "video_tracking",
    "ocr",
    "classification",
    "obb",
    "pose",
    "doc_layout",
    "managed_models",
    "managed_evaluation",
)

InferenceExecutionMode = Literal["container", "direct"]
PluginSourceTier = Literal["official", "third_party"]
InferenceCapability = Literal[
    "inference",
    "detection",
    "segmentation",
    "interactive",
    "video_tracking",
    "ocr",
    "classification",
    "obb",
    "pose",
    "doc_layout",
    "managed_models",
    "managed_evaluation",
]

_MAX_OCI_MANIFEST_BYTES = 65536
_MAX_OCI_QUALIFICATION_COMMAND_BYTES = 4096
_EXECUTABLE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "fish"})
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_NUMERIC_HOST_RE = re.compile(
    r"^(?:[0-9]+|0[xX][0-9a-fA-F]+)(?:\.(?:[0-9]+|0[xX][0-9a-fA-F]+)){0,3}$"
)
_LEGACY_OMITTED_MANIFEST_KEYS = (
    "manifest_schema",
    "display_name",
    "description",
    "homepage",
    "protocol_version",
    "execution_mode",
)

_OCI_DIGEST_REFERENCE = (
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}$"
)


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _is_forbidden_public_host(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
    except ValueError:
        return bool(_NUMERIC_HOST_RE.fullmatch(lowered))
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
        or not addr.is_global
    )


def _validate_printable_text(value: str, *, max_length: int, label: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length or _has_control_chars(value):
        raise ValueError(f"{label} must be 1..{max_length} printable characters")
    return cleaned


def _validate_display_text(
    value: str | None, *, max_length: int, label: str
) -> str | None:
    if value is None:
        return None
    return _validate_printable_text(value, max_length=max_length, label=label)


def validate_plugin_homepage(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value
        or value != value.strip()
        or len(value) > 255
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value)
    ):
        raise ValueError("homepage must be a valid https:// URL up to 255 ASCII chars")
    parsed = urlsplit(value)
    if parsed.netloc.endswith(":"):
        raise ValueError("homepage must not contain an empty port")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("homepage contains an invalid port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("homepage port out of range")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or _is_forbidden_public_host(parsed.hostname)
    ):
        raise ValueError("homepage must be a public https:// URL")
    return value


def _validate_non_shell_entrypoint(entrypoint: Sequence[str]) -> list[str]:
    if not entrypoint or len(entrypoint) > 64:
        raise ValueError("entrypoint must contain 1..64 arguments")
    if any(
        not arg or not arg.strip() or any(c in arg for c in "\x00\r\n")
        for arg in entrypoint
    ):
        raise ValueError("entrypoint arguments must be non-empty single-line strings")
    head = entrypoint[0]
    if _EXECUTABLE_NAME.fullmatch(head) is None or head.lower() in _SHELL_EXECUTABLES:
        raise ValueError("entrypoint must not invoke a shell")
    if head.lower().startswith("python") and any(
        arg == "-c"
        or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])
        for arg in entrypoint[1:]
    ):
        raise ValueError("entrypoint must not invoke python -c")
    return list(entrypoint)


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


class ExtensionResourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_mb: StrictInt = Field(ge=16, le=65536)


class InferenceComputeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: Literal["cpu", "cuda"]
    gpu_memory_mb: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_compute(self) -> InferenceComputeV1:
        if self.device == "cpu" and self.gpu_memory_mb != 0:
            raise ValueError("CPU compute must have gpu_memory_mb == 0")
        return self


class PluginResourceRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: Literal["cpu", "cuda"]
    memory_mb: int | None = Field(default=None, gt=0, strict=True)
    gpu_memory_mb: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_device(self):
        if self.device == "cpu" and self.gpu_memory_mb != 0:
            raise ValueError("CPU must not have a GPU memory budget")
        return self


class ManagedModelCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    framework: StrictStr = Field(min_length=1, max_length=64)
    format: StrictStr | None = Field(
        min_length=1,
        max_length=64,
        description="Null identifies a native checkpoint without a separate export format.",
    )
    runtime_device: Literal["cpu", "cuda"]

    @model_validator(mode="after")
    def validate_canonical(self):
        if self.framework.strip() != self.framework:
            raise ValueError("framework must be canonical")
        if self.format is not None and self.format.strip() != self.format:
            raise ValueError("format must be canonical")
        return self


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


class InferenceExtensionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: InferenceExecutionMode = "container"
    models: list[PluginModelCapability] = Field(min_length=1, max_length=128)
    pools: dict[str, PluginPoolTopology] = Field(min_length=1, max_length=128)
    compute: InferenceComputeV1
    sdk_compatibility: StrictStr | None = Field(
        default=None, min_length=1, max_length=255
    )
    asset_manifest_sha256: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    allowed_asset_sources: list[StrictStr] = Field(default_factory=list)
    managed_model_compatibility: list[ManagedModelCompatibility] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _validate_extension(self) -> InferenceExtensionV1:
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("model ids must be unique")
        if any(
            not pool or len(pool) > 128 or pool.strip() != pool for pool in self.pools
        ):
            raise ValueError("pool ids must be nonempty and canonical")
        if self.sdk_compatibility is not None and not self.sdk_compatibility.strip():
            raise ValueError("sdk_compatibility must not be blank")
        if any(not item or not item.strip() for item in self.allowed_asset_sources):
            raise ValueError("allowed_asset_sources must not contain blank entries")
        if any(
            item.runtime_device != self.compute.device
            for item in self.managed_model_compatibility
        ):
            raise ValueError(
                "managed_model_compatibility runtime_device must match compute.device"
            )
        return self


class InferenceExtensionManifestV1(BaseModel):
    """Layered yoao-extension-manifest/v1 contract for extension_point='inference'."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema: Literal["yoao-extension-manifest/v1"]
    extension_point: Literal["inference"]
    plugin_id: StrictStr = Field(min_length=1, max_length=128)
    version: StrictStr = Field(min_length=1, max_length=128)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    publisher: StrictStr = Field(min_length=1, max_length=255)
    license: StrictStr = Field(min_length=1, max_length=255)
    protocol_version: Literal["yoao-inference/v2"]
    entrypoint: list[StrictStr] | None = None
    capabilities: list[InferenceCapability] = Field(min_length=1, max_length=16)
    resource: ExtensionResourceV1 | None = None
    extension: InferenceExtensionV1
    artifact: StrictStr | None = None

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        if not _PLUGIN_ID_RE.fullmatch(value):
            raise ValueError("plugin_id must match ^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=128, label="version")

    @field_validator("publisher")
    @classmethod
    def _validate_publisher(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=255, label="publisher")

    @field_validator("license")
    @classmethod
    def _validate_license(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=255, label="license")

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=64, label="display_name")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=280, label="description")

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, value: str | None) -> str | None:
        return validate_plugin_homepage(value)

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_non_shell_entrypoint(value)

    @field_validator("artifact")
    @classmethod
    def _validate_artifact(cls, value: str | None) -> str | None:
        return None if value is None else validate_oci_digest_reference(value)

    @model_validator(mode="after")
    def _validate_rules(self) -> InferenceExtensionManifestV1:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must not contain duplicates")
        if "inference" not in self.capabilities:
            raise ValueError("inference manifest must include 'inference' capability")
        if ("managed_models" in self.capabilities) != bool(
            self.extension.managed_model_compatibility
        ):
            raise ValueError(
                "managed_models capability and managed_model_compatibility declarations must agree"
            )
        if (
            "managed_evaluation" in self.capabilities
            and "managed_models" not in self.capabilities
        ):
            raise ValueError(
                "managed_evaluation capability requires managed_models capability"
            )
        if self.extension.execution_mode == "container":
            if not self.entrypoint:
                raise ValueError("container inference manifest requires entrypoint")
            if self.resource is None:
                raise ValueError(
                    "container inference manifest requires resource.memory_mb"
                )
            if self.extension.asset_manifest_sha256 is None:
                raise ValueError(
                    "container inference manifest requires extension.asset_manifest_sha256"
                )
            if (
                self.extension.compute.device == "cuda"
                and self.extension.compute.gpu_memory_mb <= 0
            ):
                raise ValueError(
                    "container CUDA inference manifest requires extension.compute.gpu_memory_mb > 0"
                )
        elif self.artifact is not None:
            raise ValueError("direct inference manifest must not carry an OCI artifact")
        return self

    def _projected_fields(self) -> dict[str, Any]:
        return {
            "manifest_schema": self.manifest_schema,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "homepage": self.homepage,
            "publisher": self.publisher,
            "license": self.license,
            "protocol_version": self.protocol_version,
            "execution_mode": self.extension.execution_mode,
            "sdk_compatibility": self.extension.sdk_compatibility,
            "entrypoint": self.entrypoint,
            "capabilities": list(self.capabilities),
            "models": [
                item.model_dump(mode="python") for item in self.extension.models
            ],
            "pools": {
                key: value.model_dump(mode="python")
                for key, value in self.extension.pools.items()
            },
            "resource": {
                "device": self.extension.compute.device,
                "memory_mb": self.resource.memory_mb if self.resource else None,
                "gpu_memory_mb": self.extension.compute.gpu_memory_mb,
            },
            "asset_manifest_sha256": self.extension.asset_manifest_sha256,
            "managed_model_compatibility": [
                item.model_dump(mode="python")
                for item in self.extension.managed_model_compatibility
            ],
            "allowed_asset_sources": list(self.extension.allowed_asset_sources),
        }

    def to_plugin_image_manifest(self) -> PluginImageManifest:
        if self.artifact is not None:
            raise ValueError("image manifest must not embed artifact")
        return PluginImageManifest.model_validate(self._projected_fields())

    def to_plugin_manifest(self, *, artifact: str | None = None) -> PluginManifest:
        if (
            self.artifact is not None
            and artifact is not None
            and self.artifact != artifact
        ):
            raise ValueError("manifest already embeds a different artifact")
        resolved_artifact = artifact if artifact is not None else self.artifact
        if self.extension.execution_mode == "direct":
            if resolved_artifact is not None:
                raise ValueError(
                    "direct inference manifest cannot bind an OCI artifact"
                )
            return PluginManifest.model_validate(
                {**self._projected_fields(), "artifact": None}
            )
        if resolved_artifact is None:
            raise ValueError(
                "container inference manifest requires a bound OCI artifact"
            )
        validated = validate_oci_digest_reference(resolved_artifact)
        return PluginManifest.model_validate(
            {**self._projected_fields(), "artifact": validated}
        )


class PluginImageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema: Literal["yoao-extension-manifest/v1"] | None = None
    plugin_id: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=128)
    version: str = Field(min_length=1, max_length=128)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    publisher: str = Field(min_length=1, max_length=255)
    license: str = Field(min_length=1, max_length=255)
    protocol_version: Literal["yoao-inference/v2"] | None = None
    execution_mode: InferenceExecutionMode = "container"
    sdk_compatibility: str | None = Field(default=None, min_length=1, max_length=255)
    entrypoint: list[str] | None = None
    capabilities: list[InferenceCapability] = Field(min_length=1, max_length=16)
    models: list[PluginModelCapability] = Field(min_length=1, max_length=128)
    pools: dict[str, PluginPoolTopology] = Field(min_length=1, max_length=128)
    resource: PluginResourceRequirements
    asset_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    managed_model_compatibility: list[ManagedModelCompatibility] = Field(
        default_factory=list
    )
    allowed_asset_sources: list[str] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=128, label="version")

    @field_validator("publisher")
    @classmethod
    def _validate_publisher(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=255, label="publisher")

    @field_validator("license")
    @classmethod
    def _validate_license(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=255, label="license")

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=64, label="display_name")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=280, label="description")

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, value: str | None) -> str | None:
        return validate_plugin_homepage(value)

    @model_validator(mode="after")
    def validate_intent(self):
        if self.sdk_compatibility is not None and not self.sdk_compatibility.strip():
            raise ValueError("sdk_compatibility must not be blank")
        if self.execution_mode == "container":
            if not self.entrypoint:
                raise ValueError("container manifest requires entrypoint")
            if self.resource.memory_mb is None:
                raise ValueError("container manifest requires resource.memory_mb")
            if self.resource.device == "cuda" and self.resource.gpu_memory_mb <= 0:
                raise ValueError(
                    "CUDA requires a GPU memory budget; CPU must not have one"
                )
            if self.asset_manifest_sha256 is None:
                raise ValueError("container manifest requires asset_manifest_sha256")
        if self.entrypoint is not None and (
            not self.entrypoint
            or any(not item or not item.strip() for item in self.entrypoint)
        ):
            raise ValueError("entrypoint must contain nonempty arguments")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        if "inference" not in self.capabilities:
            raise ValueError("inference capability is required")
        if (
            "managed_evaluation" in self.capabilities
            and "managed_models" not in self.capabilities
        ):
            raise ValueError("managed_evaluation requires managed_models capability")
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

    @model_serializer(mode="wrap")
    def _serialize_compat(self, handler):
        data = handler(self)
        if (
            self.manifest_schema is None
            and self.execution_mode == "container"
            and self.display_name is None
            and self.description is None
            and self.homepage is None
            and self.protocol_version is None
        ):
            for key in _LEGACY_OMITTED_MANIFEST_KEYS:
                data.pop(key, None)
        return data

    def bind_artifact(self, artifact: str) -> PluginManifest:
        if self.execution_mode == "direct":
            raise ValueError("direct manifest cannot bind an OCI artifact")
        validated = validate_oci_digest_reference(artifact)
        return PluginManifest.model_validate(
            {**self.model_dump(mode="python"), "artifact": validated}
        )


class PluginManifest(PluginImageManifest):
    artifact: str | None = None

    @model_validator(mode="after")
    def validate_artifact_for_mode(self):
        if self.execution_mode == "container":
            if self.artifact is None:
                raise ValueError("container manifest requires a digest-pinned artifact")
            validate_oci_digest_reference(self.artifact)
        elif self.artifact is not None:
            raise ValueError("direct manifest must not declare an OCI artifact")
        return self


def parse_oci_plugin_manifest(raw: object, *, artifact: str) -> PluginManifest:
    """Parse an OCI manifest label (v1 layered or legacy flat) and bind its digest."""
    validated_artifact = validate_oci_digest_reference(artifact)
    parsed = _loads_strict_json(
        raw,
        max_bytes=_MAX_OCI_MANIFEST_BYTES,
        label=PLUGIN_MANIFEST_OCI_LABEL,
    )
    if not isinstance(parsed, dict):
        raise ValueError(f"{PLUGIN_MANIFEST_OCI_LABEL} must contain a JSON object")
    if "manifest_schema" in parsed:
        if "artifact" in parsed:
            raise ValueError(
                "image manifest must not embed artifact before installation"
            )
        layered = InferenceExtensionManifestV1.model_validate(parsed)
        if layered.extension.execution_mode != "container":
            raise ValueError(
                "OCI image label manifest must use execution_mode='container'"
            )
        return layered.to_plugin_manifest(artifact=validated_artifact)
    return PluginImageManifest.model_validate(parsed).bind_artifact(validated_artifact)


def parse_inference_manifest(
    raw: object, *, artifact: str | None = None
) -> PluginManifest:
    """Parse either yoao-extension-manifest/v1 or legacy/projected flat manifests."""
    parsed = (
        _loads_strict_json(
            raw,
            max_bytes=_MAX_OCI_MANIFEST_BYTES,
            label="inference manifest",
        )
        if isinstance(raw, str)
        else raw
    )
    if not isinstance(parsed, dict):
        raise ValueError("inference manifest must be a JSON object")
    schema_marker = parsed.get("manifest_schema")
    if schema_marker is not None and schema_marker != EXTENSION_MANIFEST_SCHEMA:
        raise ValueError(f"unsupported manifest_schema: {schema_marker!r}")
    if "extension" in parsed or "extension_point" in parsed:
        layered = InferenceExtensionManifestV1.model_validate(parsed)
        return layered.to_plugin_manifest(artifact=artifact)
    if artifact is not None:
        if "artifact" in parsed and parsed["artifact"] != artifact:
            raise ValueError("manifest already embeds a different artifact")
        flat_payload = {k: v for k, v in parsed.items() if k != "artifact"}
        return PluginImageManifest.model_validate(flat_payload).bind_artifact(artifact)
    return PluginManifest.model_validate(parsed)


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
    execution_mode: InferenceExecutionMode = "container"
    source_tier: PluginSourceTier = "third_party"

    @model_validator(mode="after")
    def validate_binding(self):
        if self.execution_mode != self.manifest.execution_mode:
            raise ValueError("installation execution_mode must match manifest")
        resource = self.resource
        if self.manifest.resource.device == "cuda":
            locally_arbitrated = (
                self.execution_mode == "container"
                or resource.gpu_resource_id is not None
                or resource.physical_gpu_uuid is not None
            )
            if locally_arbitrated:
                if (
                    not resource.gpu_resource_id
                    or not resource.physical_gpu_uuid
                    or resource.gpu_resource_id.strip() != resource.gpu_resource_id
                    or not resource.physical_gpu_uuid.startswith("GPU-")
                    or len(resource.physical_gpu_uuid) <= 4
                    or resource.physical_gpu_uuid.strip() != resource.physical_gpu_uuid
                    or re.fullmatch(r"cuda:(0|[1-9][0-9]*)", resource.logical_device)
                    is None
                    or self.manifest.resource.gpu_memory_mb <= 0
                ):
                    raise ValueError(
                        "CUDA requires an explicit resource, physical UUID, logical device, and GPU memory budget"
                    )
            else:
                if (
                    resource.logical_device != "cuda"
                    and re.fullmatch(r"cuda:(0|[1-9][0-9]*)", resource.logical_device)
                    is None
                ):
                    raise ValueError(
                        "Direct external CUDA requires logical_device 'cuda' or 'cuda:N'"
                    )
                if self.manifest.resource.gpu_memory_mb != 0:
                    raise ValueError(
                        "Unarbitrated direct CUDA must not declare a local GPU memory budget"
                    )
        elif (
            resource.gpu_resource_id is not None
            or resource.physical_gpu_uuid is not None
            or resource.logical_device != "cpu"
            or self.manifest.resource.gpu_memory_mb != 0
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
        if self.manifest.execution_mode != "container":
            raise ValueError(
                "PreparePluginInstallation only supports container execution_mode"
            )
        PluginInstallation(
            installation_id=self.installation_id,
            manifest=self.manifest,
            status="prepared",
            revision=1,
            resource=self.resource,
            execution_mode="container",
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


class RegisterDirectPluginRequest(BaseModel):
    """Super-admin registration request for a D1 unarbitrated direct inference plugin."""

    model_config = ConfigDict(extra="forbid")

    installation_id: UUID | None = None
    name: StrictStr = Field(min_length=1, max_length=255)
    url: StrictStr = Field(min_length=1, max_length=512)
    auth_token: StrictStr = Field(min_length=32, repr=False)
    source_tier: PluginSourceTier = "third_party"
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    publisher: StrictStr | None = None
    license: StrictStr | None = None
    asset_manifest_sha256: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    approved_managed_model_compatibility: list[ManagedModelCompatibility] = Field(
        default_factory=list
    )
    approved_managed_evaluation: StrictBool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _validate_printable_text(value, max_length=255, label="name")

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=64, label="display_name")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=280, label="description")

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, value: str | None) -> str | None:
        return validate_plugin_homepage(value)

    @field_validator("publisher")
    @classmethod
    def _validate_publisher(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=255, label="publisher")

    @field_validator("license")
    @classmethod
    def _validate_license(cls, value: str | None) -> str | None:
        return _validate_display_text(value, max_length=255, label="license")

    @model_validator(mode="after")
    def _validate_approvals(self) -> RegisterDirectPluginRequest:
        keys = [
            (item.framework, item.format, item.runtime_device)
            for item in self.approved_managed_model_compatibility
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "approved_managed_model_compatibility must not contain duplicates"
            )
        if (
            self.approved_managed_evaluation
            and not self.approved_managed_model_compatibility
        ):
            raise ValueError(
                "approved_managed_evaluation requires approved_managed_model_compatibility"
            )
        return self


class PluginActivateRequest(PluginInstallationCommand):
    receipt: PluginLaunchReceipt | None = None
    qualification: PluginLifecycleQualification | None = None


class InferencePluginSummary(BaseModel):
    """Unified read-only projection shared across container and direct inference plugins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    extension_point: Literal["inference"] = "inference"
    plugin_id: str
    version: str
    display_name: str | None = None
    description: str | None = None
    homepage: str | None = None
    source_tier: PluginSourceTier
    execution_mode: InferenceExecutionMode
    status: Literal["prepared", "active", "disabled", "retired"]
    device: Literal["cpu", "cuda"]
    locally_arbitrated_gpu: bool


class PluginInstallationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_id: UUID
    installation: PluginInstallation
    summary: InferencePluginSummary | None = None

    @model_validator(mode="after")
    def _populate_summary(self) -> PluginInstallationResponse:
        inst = self.installation
        expected = InferencePluginSummary(
            extension_point="inference",
            plugin_id=inst.manifest.plugin_id,
            version=inst.manifest.version,
            display_name=inst.manifest.display_name,
            description=inst.manifest.description,
            homepage=inst.manifest.homepage,
            source_tier=inst.source_tier,
            execution_mode=inst.execution_mode,
            status=inst.status,
            device=inst.manifest.resource.device,
            locally_arbitrated_gpu=(
                inst.resource.gpu_resource_id is not None
                or (
                    inst.execution_mode != "direct"
                    and inst.manifest.resource.device == "cuda"
                )
            ),
        )
        self.summary = expected
        return self


class PluginQuiesceResponse(PluginInstallationResponse):
    safe_to_stop: bool
    launch: PluginLaunchReceipt | None = None


class DirectPluginProbeResponse(BaseModel):
    """Read-only probe snapshot for an installed inference plugin."""

    model_config = ConfigDict(extra="forbid")

    registry_id: UUID
    installation_id: UUID
    execution_mode: InferenceExecutionMode
    status: Literal["prepared", "active", "disabled", "retired"]
    healthy: bool
    protocol_version: str | None = None
    boot_id: str | None = None
    device: Literal["cpu", "cuda"] | None = None
    observed_models: list[PluginModelCapability] = Field(default_factory=list)
    observed_pools: dict[str, PluginPoolTopology] = Field(default_factory=dict)
    observed_managed_model_compatibility: list[ManagedModelCompatibility] = Field(
        default_factory=list
    )
    observed_managed_evaluation: bool = False
    terminal_drain_supported: bool = False
    matches_manifest: bool
    mismatch_reason: str | None = None


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
