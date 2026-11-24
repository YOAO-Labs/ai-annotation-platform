"""Closed contracts for external notification channels and universal extension manifests."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from collections.abc import Mapping
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
NOTIFICATION_CHANNEL_PROTOCOL = "yoao-notification-channel/v1"
NOTIFICATION_CHANNEL_EVENT_VERSION = "1.0"

CHANNEL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
OCI_DIGEST_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
ENTRYPOINT_HEAD_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
DOMAIN_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

RESERVED_CHANNEL_IDS = frozenset({"in_app", "toast", "email", "external"})
SOURCE_TIERS = ("official", "third_party")
CHANNEL_STATUSES = ("prepared", "active", "disabled", "retired")
CHANNEL_MODES = ("direct", "managed")
CHANNEL_CAPABILITIES = frozenset({"deliver", "verify_address"})
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "fish"})

MAX_ADDRESS_SCHEMA_BYTES = 8192
MAX_ADDRESS_SCHEMA_PROPERTIES = 8
MAX_ADDRESS_SCHEMA_ENUM = 32
MAX_RECIPIENT_ADDRESS_BYTES = 2048
MAX_DELIVER_REQUEST_BYTES = 32768
MAX_HEALTH_RESPONSE_BYTES = 4096
MAX_SETUP_RESPONSE_BYTES = 16384
MAX_DELIVER_RESPONSE_BYTES = 4096
MAX_VERIFY_RESPONSE_BYTES = 4096
MIN_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 3600

_ALLOWED_SCHEMA_TOP_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "title", "description"}
)
_ALLOWED_PROP_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
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


def validate_channel_id(value: str) -> str:
    if type(value) is not str or not CHANNEL_ID_PATTERN.fullmatch(value):
        raise ValueError("channel_id must match ^[a-z][a-z0-9_-]{1,31}$")
    if value in RESERVED_CHANNEL_IDS:
        raise ValueError(f"channel_id {value!r} is reserved")
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


def validate_locale(value: str) -> str:
    if type(value) is not str or not LOCALE_PATTERN.fullmatch(value):
        raise ValueError("locale must be a valid BCP-47 language tag")
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
        raise ValueError("entrypoint must not invoke python -c")
    return items


def _strict_scalar_matches(value: Any, scalar_type: str) -> bool:
    if scalar_type == "string":
        return type(value) is str and not _has_control_chars(value)
    if scalar_type == "integer":
        return type(value) is int
    if scalar_type == "boolean":
        return type(value) is bool
    return False


def validate_address_schema(schema: Any) -> dict[str, Any]:
    """Validate the closed, ReDoS-free JSON Schema subset from ADR-0187 §3.2."""
    if not isinstance(schema, dict):
        raise ValueError("address_schema must be a JSON object")
    try:
        encoded = _canonical_json_bytes(schema)
    except (TypeError, ValueError) as exc:
        raise ValueError("address_schema must be strict finite JSON") from exc
    if len(encoded) > MAX_ADDRESS_SCHEMA_BYTES:
        raise ValueError("address_schema exceeds 8192 bytes")

    extra_top = set(schema) - _ALLOWED_SCHEMA_TOP_KEYS
    if extra_top:
        raise ValueError(
            f"address_schema contains forbidden keywords: {sorted(extra_top)}"
        )
    if schema.get("type") != "object":
        raise ValueError("address_schema.type must be 'object'")
    if schema.get("additionalProperties") is not False:
        raise ValueError("address_schema.additionalProperties must be false")

    for meta_key in ("title", "description"):
        if meta_key in schema:
            val = schema[meta_key]
            if type(val) is not str or len(val) > 280 or _has_control_chars(val):
                raise ValueError(f"address_schema.{meta_key} must be printable text")

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not (
        1 <= len(properties) <= MAX_ADDRESS_SCHEMA_PROPERTIES
    ):
        raise ValueError(
            f"address_schema.properties must define 1..{MAX_ADDRESS_SCHEMA_PROPERTIES} properties"
        )

    for prop_name, prop_def in properties.items():
        if (
            type(prop_name) is not str
            or not prop_name
            or len(prop_name) > 64
            or _has_control_chars(prop_name)
        ):
            raise ValueError("address_schema property name is invalid")
        if not isinstance(prop_def, dict):
            raise ValueError(f"address_schema.properties.{prop_name} must be an object")
        extra_prop = set(prop_def) - _ALLOWED_PROP_KEYS
        if extra_prop:
            raise ValueError(
                f"address_schema property {prop_name!r} has forbidden keywords: {sorted(extra_prop)}"
            )
        prop_type = prop_def.get("type")
        if prop_type not in _ALLOWED_SCALAR_TYPES:
            raise ValueError(
                f"address_schema property {prop_name!r} type must be string, integer, or boolean"
            )
        for meta_key in ("title", "description"):
            if meta_key in prop_def:
                val = prop_def[meta_key]
                if type(val) is not str or len(val) > 280 or _has_control_chars(val):
                    raise ValueError(
                        f"address_schema.{prop_name}.{meta_key} must be printable text"
                    )
        if prop_type == "string":
            if "minimum" in prop_def or "maximum" in prop_def:
                raise ValueError("string property must not declare minimum/maximum")
            min_len = prop_def.get("minLength")
            max_len = prop_def.get("maxLength")
            if min_len is not None and (type(min_len) is not int or min_len < 0):
                raise ValueError("minLength must be a non-negative integer")
            if max_len is not None and (type(max_len) is not int or max_len < 0):
                raise ValueError("maxLength must be a non-negative integer")
            if min_len is not None and max_len is not None and min_len > max_len:
                raise ValueError("minLength must not exceed maxLength")
        elif prop_type == "integer":
            if "minLength" in prop_def or "maxLength" in prop_def:
                raise ValueError(
                    "integer property must not declare minLength/maxLength"
                )
            minimum = prop_def.get("minimum")
            maximum = prop_def.get("maximum")
            if minimum is not None and type(minimum) is not int:
                raise ValueError("minimum must be an integer")
            if maximum is not None and type(maximum) is not int:
                raise ValueError("maximum must be an integer")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("minimum must not exceed maximum")
        else:
            for numeric_key in ("minLength", "maxLength", "minimum", "maximum"):
                if numeric_key in prop_def:
                    raise ValueError(f"boolean property must not declare {numeric_key}")

        if "enum" in prop_def:
            enum_values = prop_def["enum"]
            if (
                not isinstance(enum_values, list)
                or not (1 <= len(enum_values) <= MAX_ADDRESS_SCHEMA_ENUM)
                or any(
                    not _strict_scalar_matches(item, prop_type) for item in enum_values
                )
            ):
                raise ValueError(
                    f"address_schema.{prop_name}.enum must hold 1..{MAX_ADDRESS_SCHEMA_ENUM} {prop_type} values"
                )

    if "required" in schema:
        required = schema["required"]
        if (
            not isinstance(required, list)
            or any(type(name) is not str for name in required)
            or len(required) != len(set(required))
            or not set(required).issubset(properties)
        ):
            raise ValueError(
                "address_schema.required must list unique defined property names"
            )
    return schema


def validate_recipient_address(
    address: Any, address_schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate recipient_address against the validated closed schema and 2 KiB cap."""
    if not isinstance(address, dict):
        raise ValueError("recipient_address must be a JSON object")
    try:
        encoded = _canonical_json_bytes(address)
    except (TypeError, ValueError) as exc:
        raise ValueError("recipient_address must be strict finite JSON") from exc
    if len(encoded) > MAX_RECIPIENT_ADDRESS_BYTES:
        raise ValueError("recipient_address exceeds 2048 bytes")

    properties = address_schema.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError("invalid channel address_schema")

    extra_keys = set(address) - set(properties)
    if extra_keys:
        raise ValueError(
            f"recipient_address contains undeclared keys: {sorted(extra_keys)}"
        )

    for required_key in address_schema.get("required", ()):
        if required_key not in address:
            raise ValueError(f"recipient_address missing required key: {required_key}")

    for key, value in address.items():
        prop_def = properties[key]
        prop_type = prop_def["type"]
        if not _strict_scalar_matches(value, prop_type):
            raise ValueError(f"recipient_address.{key} must be a valid {prop_type}")
        if prop_type == "string":
            if "minLength" in prop_def and len(value) < prop_def["minLength"]:
                raise ValueError(f"recipient_address.{key} is shorter than minLength")
            if "maxLength" in prop_def and len(value) > prop_def["maxLength"]:
                raise ValueError(f"recipient_address.{key} exceeds maxLength")
        elif prop_type == "integer":
            if "minimum" in prop_def and value < prop_def["minimum"]:
                raise ValueError(f"recipient_address.{key} is below minimum")
            if "maximum" in prop_def and value > prop_def["maximum"]:
                raise ValueError(f"recipient_address.{key} exceeds maximum")
        if "enum" in prop_def and not any(
            type(value) is type(candidate) and value == candidate
            for candidate in prop_def["enum"]
        ):
            raise ValueError(f"recipient_address.{key} is not in allowed enum")

    return dict(address)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtensionResourceV1(_ClosedModel):
    memory_mb: StrictInt = Field(gt=0)


class NotificationChannelExtensionBlock(_ClosedModel):
    channel_id: StrictStr
    address_schema: dict[str, Any]
    allowed_egress_hosts: tuple[StrictStr, ...] = ()

    @field_validator("channel_id")
    @classmethod
    def _valid_channel_id(cls, value: str) -> str:
        return validate_channel_id(value)

    @field_validator("address_schema")
    @classmethod
    def _valid_address_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_address_schema(value)

    @field_validator("allowed_egress_hosts", mode="before")
    @classmethod
    def _valid_egress_hosts(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("allowed_egress_hosts must be a list of hostnames")
        if len(value) > 32:
            raise ValueError("allowed_egress_hosts has too many entries")
        normalized = tuple(validate_egress_host(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_egress_hosts must not contain duplicates")
        return normalized


class ExtensionManifestV1(_ClosedModel):
    """Image-embedded universal extension manifest (without artifact or source_tier)."""

    manifest_schema: Literal["yoao-extension-manifest/v1"]
    extension_point: Literal["notification_channel"]
    plugin_id: StrictStr = Field(min_length=1, max_length=128)
    version: StrictStr = Field(min_length=1, max_length=128)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    publisher: StrictStr = Field(min_length=1, max_length=255)
    license: StrictStr = Field(min_length=1, max_length=255)
    protocol_version: Literal["yoao-notification-channel/v1"]
    entrypoint: tuple[StrictStr, ...]
    capabilities: tuple[StrictStr, ...]
    resource: ExtensionResourceV1
    extension: NotificationChannelExtensionBlock

    @field_validator("plugin_id")
    @classmethod
    def _valid_plugin_id(cls, value: str) -> str:
        if not PLUGIN_ID_PATTERN.fullmatch(value):
            raise ValueError("plugin_id must match ^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
        return value

    @field_validator("version", "publisher", "license")
    @classmethod
    def _printable_metadata(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or _has_control_chars(value):
            raise ValueError("metadata field must be non-empty printable text")
        return cleaned

    @field_validator("display_name")
    @classmethod
    def _valid_display_name(cls, value: str | None) -> str | None:
        return validate_display_name(value) if value is not None else None

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str | None) -> str | None:
        return validate_description(value)

    @field_validator("homepage")
    @classmethod
    def _valid_homepage(cls, value: str | None) -> str | None:
        return validate_homepage_url(value)

    @field_validator("entrypoint", mode="before")
    @classmethod
    def _valid_entrypoint(cls, value: Any) -> tuple[str, ...]:
        return validate_entrypoint(value)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _valid_capabilities(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("capabilities must be a non-empty list")
        items = tuple(value)
        if any(type(cap) is not str for cap in items) or len(items) != len(set(items)):
            raise ValueError("capabilities must be unique strings")
        unknown = set(items) - CHANNEL_CAPABILITIES
        if unknown:
            raise ValueError(f"unsupported channel capabilities: {sorted(unknown)}")
        if "deliver" not in items:
            raise ValueError("notification_channel capabilities must include 'deliver'")
        return items

    def bind_artifact(self, artifact: str) -> BoundExtensionManifestV1:
        return BoundExtensionManifestV1(
            **self.model_dump(mode="python"),
            artifact=artifact,
        )


class BoundExtensionManifestV1(ExtensionManifestV1):
    artifact: StrictStr

    @field_validator("artifact")
    @classmethod
    def _valid_artifact(cls, value: str) -> str:
        if not OCI_DIGEST_PATTERN.fullmatch(value):
            raise ValueError("artifact must be a pinned @sha256: OCI digest reference")
        return value


class ChannelSetupResponse(_ClosedModel):
    protocol_version: Literal["yoao-notification-channel/v1"]
    channel_id: StrictStr
    display_name: StrictStr
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    capabilities: tuple[StrictStr, ...]
    address_schema: dict[str, Any]

    @field_validator("channel_id")
    @classmethod
    def _valid_channel_id(cls, value: str) -> str:
        return validate_channel_id(value)

    @field_validator("display_name")
    @classmethod
    def _valid_display_name(cls, value: str) -> str:
        return validate_display_name(value)

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str | None) -> str | None:
        return validate_description(value)

    @field_validator("homepage")
    @classmethod
    def _valid_homepage(cls, value: str | None) -> str | None:
        return validate_homepage_url(value)

    @field_validator("capabilities", mode="before")
    @classmethod
    def _valid_capabilities(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("capabilities must be a non-empty list")
        items = tuple(value)
        if any(type(cap) is not str for cap in items) or len(items) != len(set(items)):
            raise ValueError("capabilities must be unique strings")
        unknown = set(items) - CHANNEL_CAPABILITIES
        if unknown:
            raise ValueError(f"unsupported channel capabilities: {sorted(unknown)}")
        if "deliver" not in items:
            raise ValueError("notification channel capabilities must include 'deliver'")
        return items

    @field_validator("address_schema")
    @classmethod
    def _valid_address_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_address_schema(value)


class NotificationChannelEnvelope(_ClosedModel):
    """ADR-0025 5-field top-level envelope shape for external notification channels."""

    event_version: Literal["1.0"] = NOTIFICATION_CHANNEL_EVENT_VERSION
    event: StrictStr = Field(min_length=1, max_length=60)
    delivery_id: uuid.UUID
    occurred_at: StrictStr = Field(min_length=1, max_length=64)
    data: dict[str, Any]


class ChannelDeliverRequest(_ClosedModel):
    protocol_version: Literal["yoao-notification-channel/v1"] = (
        NOTIFICATION_CHANNEL_PROTOCOL
    )
    channel_id: StrictStr
    envelope: NotificationChannelEnvelope
    recipient_address: dict[str, Any]
    locale: StrictStr = "zh-CN"

    @field_validator("channel_id")
    @classmethod
    def _valid_channel_id(cls, value: str) -> str:
        return validate_channel_id(value)

    @field_validator("locale")
    @classmethod
    def _valid_locale(cls, value: str) -> str:
        return validate_locale(value)

    @model_validator(mode="after")
    def _within_size_cap(self) -> ChannelDeliverRequest:
        if (
            len(_canonical_json_bytes(self.recipient_address))
            > MAX_RECIPIENT_ADDRESS_BYTES
        ):
            raise ValueError("recipient_address exceeds 2048 bytes")
        if len(self.encode_json_bytes()) > MAX_DELIVER_REQUEST_BYTES:
            raise ValueError("deliver request exceeds 32768 bytes")
        return self

    def encode_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


class ChannelVerifyAddressRequest(_ClosedModel):
    protocol_version: Literal["yoao-notification-channel/v1"] = (
        NOTIFICATION_CHANNEL_PROTOCOL
    )
    channel_id: StrictStr
    verification_id: uuid.UUID
    recipient_address: dict[str, Any]
    locale: StrictStr = "zh-CN"

    @field_validator("channel_id")
    @classmethod
    def _valid_channel_id(cls, value: str) -> str:
        return validate_channel_id(value)

    @field_validator("locale")
    @classmethod
    def _valid_locale(cls, value: str) -> str:
        return validate_locale(value)

    @model_validator(mode="after")
    def _within_size_cap(self) -> ChannelVerifyAddressRequest:
        if (
            len(_canonical_json_bytes(self.recipient_address))
            > MAX_RECIPIENT_ADDRESS_BYTES
        ):
            raise ValueError("recipient_address exceeds 2048 bytes")
        return self

    def encode_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


class ChannelDeliveryOutcome(_ClosedModel):
    outcome: Literal["delivered", "permanent", "retryable", "auth_error"]
    provider_message_id: StrictStr | None = None
    reason: StrictStr | None = None
    retry_after_seconds: StrictInt | None = None


_ALLOWED_PAYLOAD_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "task.approved": frozenset({"task_display_id", "project_name", "actor_name"}),
    "task.rejected": frozenset(
        {"task_display_id", "project_name", "reject_reason_type", "actor_name"}
    ),
    "task.reopened": frozenset(
        {"task_display_id", "project_name", "actor_name", "reopened_count"}
    ),
    "batch.rejected": frozenset(
        {"batch_display_id", "batch_name", "project_name", "affected_tasks", "bulk"}
    ),
    "batch.review_reopened": frozenset(
        {"batch_display_id", "batch_name", "project_name", "from_status"}
    ),
    "batch.admin_locked": frozenset({"batch_display_id", "batch_name", "project_name"}),
    "batch.admin_unlocked": frozenset(
        {"batch_display_id", "batch_name", "project_name"}
    ),
    "batch.unarchived": frozenset(
        {"batch_display_id", "batch_name", "project_name", "from_status"}
    ),
    "feedback.reply_created": frozenset({"project_name", "source", "actor_name"}),
    "feedback.status_changed": frozenset(
        {"project_name", "source", "actor_name", "from_status", "to_status"}
    ),
    "feedback.comment_mentioned": frozenset({"project_name", "source", "actor_name"}),
    "annotation.comment_mentioned": frozenset({"project_name", "source", "actor_name"}),
    "bug_report.commented": frozenset(
        {"display_id", "actor_name", "actor_role", "reopen"}
    ),
    "bug_report.reopened": frozenset(
        {"display_id", "actor_name", "actor_role", "reopen", "reopen_count"}
    ),
    "bug_report.status_changed": frozenset(
        {"display_id", "from_status", "to_status", "actor_name"}
    ),
    "failed_prediction.retry.started": frozenset({"project_name", "retry_job_id"}),
    "failed_prediction.retry.succeeded": frozenset({"project_name", "retry_job_id"}),
    "failed_prediction.retry.failed": frozenset({"project_name", "retry_job_id"}),
    "export.ready": frozenset(
        {"project_display_id", "project_name", "targets", "format", "file_count"}
    ),
    "export.failed": frozenset(
        {"project_display_id", "project_name", "targets", "format"}
    ),
    "job.completed": frozenset({"kind", "status", "project_name"}),
    "job.failed": frozenset({"kind", "status", "project_name"}),
    "job.cancelled": frozenset({"kind", "status", "project_name"}),
}


def _safe_projected_value(value: Any) -> Any:
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        cleaned = value.strip()
        if not cleaned or _has_control_chars(cleaned):
            return None
        return cleaned[:160]
    if isinstance(value, list) and len(value) <= 16:
        items = [
            item.strip()[:64]
            for item in value
            if type(item) is str and item.strip() and not _has_control_chars(item)
        ]
        return items if len(items) == len(value) else None
    return None


def _derive_action_url(
    *,
    target_type: str,
    target_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
) -> str | None:
    if target_type == "task" and project_id is not None:
        return f"/projects/{project_id}/workbench/{target_id}"
    if target_type in {"feedback", "annotation_comment"} and project_id is not None:
        if task_id is not None:
            return f"/projects/{project_id}/workbench/{task_id}"
        return f"/projects/{project_id}"
    if target_type == "batch" and project_id is not None:
        return f"/projects/{project_id}/batches"
    if target_type == "failed_prediction" and project_id is not None:
        return f"/projects/{project_id}/predictions"
    if target_type == "export" and project_id is not None:
        return f"/projects/{project_id}/exports"
    if target_type == "bug_report":
        return f"/bug-reports/{target_id}"
    if target_type == "async_job":
        return f"/jobs/{target_id}"
    return None


def project_external_envelope_data(
    *,
    notification_type: str,
    notification_id: uuid.UUID | None,
    target_type: str,
    target_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build strictly whitelisted, scrubbed external envelope data per ADR-0187 §3.2."""
    data: dict[str, Any] = {
        "target_type": target_type,
        "target_id": str(target_id),
    }
    if notification_id is not None:
        data["notification_id"] = str(notification_id)
    if project_id is not None:
        data["project_id"] = str(project_id)
    if task_id is not None:
        data["task_id"] = str(task_id)

    action_url = _derive_action_url(
        target_type=target_type,
        target_id=target_id,
        project_id=project_id,
        task_id=task_id,
    )
    if action_url is not None:
        data["action_url"] = action_url

    allowed_keys = _ALLOWED_PAYLOAD_FIELDS_BY_TYPE.get(notification_type, frozenset())
    if isinstance(payload, Mapping) and allowed_keys:
        for key in sorted(allowed_keys):
            if key in payload:
                safe_val = _safe_projected_value(payload[key])
                if safe_val is not None:
                    data[key] = safe_val
    return data
