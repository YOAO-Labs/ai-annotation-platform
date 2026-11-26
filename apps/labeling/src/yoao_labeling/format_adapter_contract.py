"""Closed contracts for external format adapter plugins and universal extension manifests."""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

EXTENSION_MANIFEST_SCHEMA = "yoao-extension-manifest/v1"
FORMAT_ADAPTER_PROTOCOL = "yoao-format-adapter/v1"

FORMAT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
OCI_DIGEST_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
ENTRYPOINT_HEAD_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
DOMAIN_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

BUILTIN_FORMAT_IDS = frozenset(
    {
        "native_json",
        "coco",
        "yolo",
        "yolo-det",
        "yolo-obb",
        "yolo-seg",
        "label-studio-brush",
        "binary-png",
        "indexed-png",
        "video_json",
        "yolo-frames-det",
        "yolo-frames-seg",
        "coco-frames-seg",
        "davis",
        "youtube-vos",
        "mots",
        "mot",
        "kitti",
        "nuscenes",
        "coco-multicamera",
        "pointmask",
        "voc",
    }
)

SOURCE_TIERS = ("official", "third_party")
PLUGIN_STATUSES = ("prepared", "active", "disabled", "retired")
PLUGIN_MODES = ("direct", "managed")
FORMAT_ADAPTER_CAPABILITIES = frozenset({"import", "export"})
FORMAT_ADAPTER_MEDIA_TYPES = frozenset({"image", "video", "lidar"})
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "fish"})

MAX_OPTION_SCHEMA_BYTES = 8192
MAX_OPTION_SCHEMA_PROPERTIES = 8
MAX_OPTION_SCHEMA_ENUM = 32
MAX_HEALTH_RESPONSE_BYTES = 4096
MAX_SETUP_RESPONSE_BYTES = 16384
MAX_IMPORT_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_IMPORT_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_EXPORT_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PARSED_ITEMS = 10_000
MAX_EXPORT_FILES = 10_000

_ALLOWED_SCHEMA_TOP_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "title", "description"}
)
_ALLOWED_PROP_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
    }
)
_ALLOWED_SCALAR_TYPES = frozenset({"string", "integer", "boolean"})


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def validate_format_id(value: str) -> str:
    if type(value) is not str or not FORMAT_ID_PATTERN.fullmatch(value):
        raise ValueError("format_id must match ^[a-z][a-z0-9_-]{1,63}$")
    if value in BUILTIN_FORMAT_IDS:
        raise ValueError(f"format_id {value!r} collides with a built-in format")
    return value


def validate_display_name(value: str) -> str:
    if type(value) is not str:
        raise ValueError("display_name must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 64 or _has_control_chars(value):
        raise ValueError("display_name must be 1..64 printable characters")
    return cleaned


def validate_description(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("description must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 280 or _has_control_chars(value):
        raise ValueError("description must be 1..280 printable characters")
    return cleaned


def _unwrap_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_forbidden_public_host(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        addr = _unwrap_ip(ipaddress.ip_address(lowered))
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
        or not addr.is_global
    )


def validate_homepage_url(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("homepage must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > 255
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value)
    ):
        raise ValueError("homepage must be a valid https URL up to 255 ASCII chars")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("homepage must be a valid https URL") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or _is_forbidden_public_host(hostname)
    ):
        raise ValueError(
            "homepage must be a public https:// URL without credentials or private hosts"
        )
    return value


def validate_egress_host(host: str) -> str:
    if type(host) is not str or not host or len(host) > 253 or _has_control_chars(host):
        raise ValueError("allowed_egress_hosts entries must be valid hostnames")
    if "://" in host or "/" in host or ":" in host or "*" in host:
        raise ValueError("allowed_egress_hosts entries must be plain hostnames")
    lowered = host.rstrip(".").lower()
    if _is_forbidden_public_host(lowered):
        raise ValueError(f"forbidden egress host: {host}")
    try:
        ipaddress.ip_address(lowered)
    except ValueError:
        try:
            ascii_host = lowered.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("invalid egress hostname") from exc
        if len(ascii_host) > 253 or any(
            not DOMAIN_LABEL_PATTERN.fullmatch(label) for label in ascii_host.split(".")
        ):
            raise ValueError("invalid egress hostname")
        return ascii_host
    return lowered


def validate_entrypoint(entrypoint: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(entrypoint, (list, tuple)) or not entrypoint:
        raise ValueError("entrypoint must be a non-empty string list")
    if len(entrypoint) > 64:
        raise ValueError("entrypoint has too many arguments")
    items = tuple(entrypoint)
    if any(
        type(arg) is not str or not arg or any(c in arg for c in "\x00\r\n")
        for arg in items
    ):
        raise ValueError("entrypoint arguments must be non-empty strings")
    head = items[0]
    if (
        not ENTRYPOINT_HEAD_PATTERN.fullmatch(head)
        or head.lower() in _SHELL_EXECUTABLES
    ):
        raise ValueError("entrypoint must not invoke a shell")
    if head.lower().startswith("python") and any(
        arg == "-c"
        or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])
        for arg in items[1:]
    ):
        raise ValueError("entrypoint must not use python -c")
    return items


def validate_option_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ValueError("option_schema must be an object")
    if not schema:
        return {}
    if len(_canonical_json_bytes(schema)) > MAX_OPTION_SCHEMA_BYTES:
        raise ValueError("option_schema exceeds 8 KiB")
    extra_top = set(schema) - _ALLOWED_SCHEMA_TOP_KEYS
    if extra_top:
        raise ValueError(f"option_schema contains disallowed keys: {sorted(extra_top)}")
    if schema.get("type") != "object":
        raise ValueError("option_schema.type must be 'object'")
    if schema.get("additionalProperties") is not False:
        raise ValueError("option_schema.additionalProperties must be false")
    properties = schema.get("properties")
    if (
        not isinstance(properties, dict)
        or len(properties) > MAX_OPTION_SCHEMA_PROPERTIES
    ):
        raise ValueError(
            "option_schema.properties must be an object with at most 8 keys"
        )
    for prop_name, prop_spec in properties.items():
        if type(prop_name) is not str or not prop_name or len(prop_name) > 64:
            raise ValueError("option_schema property names must be 1..64 chars")
        if not isinstance(prop_spec, dict):
            raise ValueError(f"option_schema property {prop_name!r} must be an object")
        extra_prop = set(prop_spec) - _ALLOWED_PROP_KEYS
        if extra_prop:
            raise ValueError(
                f"option_schema property {prop_name!r} has disallowed keys: {sorted(extra_prop)}"
            )
        prop_type = prop_spec.get("type")
        if prop_type not in _ALLOWED_SCALAR_TYPES:
            raise ValueError(
                f"option_schema property {prop_name!r} type must be string, integer, or boolean"
            )
        if "enum" in prop_spec:
            enum_values = prop_spec["enum"]
            if (
                not isinstance(enum_values, list)
                or not enum_values
                or len(enum_values) > MAX_OPTION_SCHEMA_ENUM
            ):
                raise ValueError(
                    f"option_schema property {prop_name!r} enum must have 1..32 values"
                )
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or any(
            type(item) is not str or item not in properties for item in required
        ):
            raise ValueError(
                "option_schema.required must reference declared properties"
            )
    return schema


def validate_safe_relative_path(path: str) -> str:
    if type(path) is not str or not path or len(path) > 255:
        raise ValueError("export file path must be 1..255 characters")
    if "\x00" in path or "\\" in path or path.startswith("/") or path.endswith("/"):
        raise ValueError(f"unsafe export file path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe export file path: {path!r}")
    return path


class FormatAdapterResourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_mb: StrictInt = Field(ge=32, le=16384)


class FormatAdapterExtensionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_id: StrictStr
    adapter_version: StrictStr = Field(min_length=1, max_length=64)
    manifest_version: StrictStr = Field(default="1", min_length=1, max_length=32)
    media_types: list[Literal["image", "video", "lidar"]] = Field(
        min_length=1, max_length=3
    )
    option_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_egress_hosts: list[StrictStr] = Field(default_factory=list, max_length=32)

    @field_validator("format_id")
    @classmethod
    def _validate_format_id(cls, value: str) -> str:
        return validate_format_id(value)

    @field_validator("media_types")
    @classmethod
    def _validate_media_types(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("media_types must not contain duplicates")
        return value

    @field_validator("option_schema")
    @classmethod
    def _validate_option_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_option_schema(value)

    @field_validator("allowed_egress_hosts")
    @classmethod
    def _validate_egress_hosts(cls, value: list[str]) -> list[str]:
        cleaned = [validate_egress_host(host) for host in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("allowed_egress_hosts must not contain duplicates")
        return cleaned


class FormatAdapterManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_schema: Literal["yoao-extension-manifest/v1"] = EXTENSION_MANIFEST_SCHEMA
    extension_point: Literal["format_adapter"] = "format_adapter"
    plugin_id: StrictStr = Field(min_length=1, max_length=128)
    version: StrictStr = Field(min_length=1, max_length=128)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    publisher: StrictStr = Field(min_length=1, max_length=255)
    license: StrictStr = Field(min_length=1, max_length=255)
    protocol_version: Literal["yoao-format-adapter/v1"] = FORMAT_ADAPTER_PROTOCOL
    entrypoint: list[StrictStr] = Field(min_length=1, max_length=64)
    capabilities: list[Literal["import", "export"]] = Field(min_length=1, max_length=2)
    resource: FormatAdapterResourceV1
    extension: FormatAdapterExtensionV1
    artifact: StrictStr | None = None

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        if not PLUGIN_ID_PATTERN.fullmatch(value):
            raise ValueError("plugin_id must match ^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
        return value

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        return None if value is None else validate_display_name(value)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return validate_description(value)

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, value: str | None) -> str | None:
        return validate_homepage_url(value)

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, value: list[str]) -> list[str]:
        return list(validate_entrypoint(value))

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("capabilities must not contain duplicates")
        return value

    @field_validator("artifact")
    @classmethod
    def _validate_artifact(cls, value: str | None) -> str | None:
        if value is not None and not OCI_DIGEST_PATTERN.fullmatch(value):
            raise ValueError(
                "artifact must be a digest-pinned OCI reference (@sha256:...)"
            )
        return value

    def bind_artifact(self, digest_ref: str) -> FormatAdapterManifestV1:
        if self.artifact is not None:
            raise ValueError(
                "image manifest must not embed artifact before installation"
            )
        if not OCI_DIGEST_PATTERN.fullmatch(digest_ref):
            raise ValueError(
                "artifact must be a digest-pinned OCI reference (@sha256:...)"
            )
        return self.model_copy(update={"artifact": digest_ref})


class FormatAdapterSetupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-format-adapter/v1"]
    format_id: StrictStr
    adapter_version: StrictStr = Field(min_length=1, max_length=64)
    manifest_version: StrictStr = Field(default="1", min_length=1, max_length=32)
    display_name: StrictStr
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    media_types: list[Literal["image", "video", "lidar"]] = Field(
        min_length=1, max_length=3
    )
    capabilities: list[Literal["import", "export"]] = Field(min_length=1, max_length=2)
    option_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("format_id")
    @classmethod
    def _validate_format_id(cls, value: str) -> str:
        return validate_format_id(value)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return validate_display_name(value)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return validate_description(value)

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, value: str | None) -> str | None:
        return validate_homepage_url(value)

    @field_validator("media_types")
    @classmethod
    def _validate_media_types(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("media_types must not contain duplicates")
        return value

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("capabilities must not contain duplicates")
        return value

    @field_validator("option_schema")
    @classmethod
    def _validate_option_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_option_schema(value)


class FormatAdapterImportParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-format-adapter/v1"] = FORMAT_ADAPTER_PROTOCOL
    format_id: StrictStr
    media_type: Literal["image", "video", "lidar"]
    staged_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    content_base64: StrictStr
    mapping: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class FormatAdapterParsedAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    class_name: StrictStr = Field(min_length=1, max_length=128)
    tool_unit_id: StrictStr = Field(min_length=1, max_length=64)
    geometry: dict[str, Any]
    source_id: StrictStr | None = Field(default=None, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)


class FormatAdapterParsedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_path: StrictStr = Field(min_length=1, max_length=512)
    width: StrictInt | None = Field(default=None, gt=0)
    height: StrictInt | None = Field(default=None, gt=0)
    losses: list[StrictStr] = Field(default_factory=list, max_length=16)
    warnings: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    annotations: list[FormatAdapterParsedAnnotation] = Field(default_factory=list)
    mask_objects: dict[str, dict[str, Any]] = Field(default_factory=dict)


class FormatAdapterImportParseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-format-adapter/v1"]
    items: list[FormatAdapterParsedItem] = Field(max_length=MAX_PARSED_ITEMS)


class FormatAdapterExportRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-format-adapter/v1"] = FORMAT_ADAPTER_PROTOCOL
    format_id: StrictStr
    media_type: Literal["image", "video", "lidar"]
    options: dict[str, Any] = Field(default_factory=dict)
    classes: list[StrictStr] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class FormatAdapterExportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr
    content_base64: StrictStr

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_safe_relative_path(value)

    def decoded_bytes(self) -> bytes:
        try:
            return base64.b64decode(self.content_base64.encode("ascii"), validate=True)
        except Exception as exc:
            raise ValueError(f"invalid base64 content for {self.path}") from exc


class FormatAdapterExportRenderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["yoao-format-adapter/v1"]
    files: list[FormatAdapterExportFile] = Field(max_length=MAX_EXPORT_FILES)

    @model_validator(mode="after")
    def _validate_unique_paths(self) -> FormatAdapterExportRenderResponse:
        seen: set[str] = set()
        for row in self.files:
            if row.path in seen:
                raise ValueError(f"duplicate export file path: {row.path}")
            seen.add(row.path)
        return self


class FormatAdapterPluginRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    format_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    source_tier: Literal["official", "third_party"]
    mode: Literal["direct", "managed"]
    status: Literal["prepared", "active", "disabled", "retired"]
    revision: int
    endpoint_url: str
    protocol_version: str
    adapter_version: str
    manifest_version: str
    media_types: list[str]
    capabilities: list[str]
    option_schema: dict[str, Any] = Field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None
    retired_at: datetime | None = None


class FormatAdapterPluginSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_id: str
    status: Literal["prepared", "active", "disabled", "retired"]
    endpoint_url: str
    auth_token_ciphertext: str | None = None
    protocol_version: str
    adapter_version: str
    manifest_version: str
    display_name: str
    media_types: list[str]
    capabilities: list[str]
    option_schema: dict[str, Any] = Field(default_factory=dict)


def validate_manifest_matches_setup(
    manifest: FormatAdapterManifestV1, setup: FormatAdapterSetupResponse
) -> None:
    if manifest.protocol_version != setup.protocol_version:
        raise ValueError("manifest protocol_version does not match /setup")
    if manifest.extension.format_id != setup.format_id:
        raise ValueError("manifest extension.format_id does not match /setup")
    if manifest.extension.adapter_version != setup.adapter_version:
        raise ValueError("manifest extension.adapter_version does not match /setup")
    if manifest.extension.manifest_version != setup.manifest_version:
        raise ValueError("manifest extension.manifest_version does not match /setup")
    if set(manifest.extension.media_types) != set(setup.media_types):
        raise ValueError("manifest extension.media_types does not match /setup")
    if set(manifest.capabilities) != set(setup.capabilities):
        raise ValueError("manifest capabilities do not match /setup")


def format_adapter_public_projection_from_row(
    row: Mapping[str, Any],
) -> FormatAdapterPluginRecord:
    return FormatAdapterPluginRecord.model_validate(dict(row))
