"""Credential-free immutable facts for Inference backend topology."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID


class TopologyContractError(ValueError):
    """A Native topology result does not match its closed public projection."""


def freeze_json(value: Any) -> Any:
    """Copy JSON data into immutable containers once, reusing frozen subtrees."""
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TopologyContractError("topology JSON object keys must be strings")
        frozen = {key: freeze_json(item) for key, item in value.items()}
        if isinstance(value, MappingProxyType) and all(
            frozen[key] is value[key] for key in frozen
        ):
            return value
        return MappingProxyType(frozen)
    if isinstance(value, tuple):
        frozen = tuple(freeze_json(item) for item in value)
        return value if all(new is old for new, old in zip(frozen, value)) else frozen
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TopologyContractError("topology facts must contain JSON values")


def to_record(value: Any) -> Any:
    """Thaw immutable facts into JSON-native records at a serialization boundary."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_record(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TopologyContractError("topology JSON object keys must be strings")
        return {key: to_record(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_record(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TopologyContractError("topology timestamps must be timezone-aware")
        return value.isoformat()
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TopologyContractError("topology facts cannot be represented as JSON")


def _frozen_mapping(
    value: Any, label: str, *, nullable: bool = False
) -> Mapping[str, Any] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, Mapping):
        raise TopologyContractError(f"invalid topology {label}")
    return freeze_json(value)


def _mapping(value: Any, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise TopologyContractError("invalid topology fact fields")
    return value


def _uuid(value: Any, *, nullable: bool = False) -> UUID | None:
    if value is None and nullable:
        return None
    if isinstance(value, UUID):
        return value
    if type(value) is str:
        try:
            parsed = UUID(value)
        except ValueError:
            parsed = None
        if parsed is not None and str(parsed) == value.lower():
            return parsed
    raise TopologyContractError("invalid topology identity")


def _datetime(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    else:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TopologyContractError("invalid topology timestamp")
    return parsed


def _required(value: Any, expected: type, label: str):
    if type(value) is not expected:
        raise TopologyContractError(f"invalid topology {label}")
    return value


def _nullable(value: Any, expected: type, label: str):
    if value is None:
        return None
    return _required(value, expected, label)


@dataclass(frozen=True, slots=True)
class RegistryPublicFacts:
    id: UUID
    name: str
    url: str
    state: str
    is_interactive: bool
    auth_method: str
    credential_configured: bool
    extra_params: Mapping[str, Any] | None
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    eviction_priority: int
    health_meta: Mapping[str, Any] | None
    source: str
    error_message: str | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, value: Any) -> RegistryPublicFacts:
        fields = frozenset(cls.__dataclass_fields__)
        row = _mapping(value, fields)
        return cls(
            id=_uuid(row["id"]),
            name=_required(row["name"], str, "name"),
            url=_required(row["url"], str, "URL"),
            state=_required(row["state"], str, "state"),
            is_interactive=_required(row["is_interactive"], bool, "interactive flag"),
            auth_method=_required(row["auth_method"], str, "auth method"),
            credential_configured=_required(
                row["credential_configured"], bool, "credential flag"
            ),
            extra_params=_frozen_mapping(
                row["extra_params"], "extra params", nullable=True
            ),
            gpu_resource_id=_nullable(row["gpu_resource_id"], str, "GPU resource"),
            vram_budget_mb=_nullable(row["vram_budget_mb"], int, "VRAM budget"),
            eviction_priority=_required(
                row["eviction_priority"], int, "eviction priority"
            ),
            health_meta=_frozen_mapping(
                row["health_meta"], "health metadata", nullable=True
            ),
            source=_required(row["source"], str, "source"),
            error_message=_nullable(row["error_message"], str, "error message"),
            last_checked_at=_datetime(row["last_checked_at"], nullable=True),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class RegistryReference:
    id: UUID

    @classmethod
    def from_record(cls, value: Any) -> RegistryReference:
        row = _mapping(value, frozenset({"id"}))
        return cls(id=_uuid(row["id"]))


@dataclass(frozen=True, slots=True)
class RegistryName:
    id: UUID
    name: str

    @classmethod
    def from_record(cls, value: Any) -> RegistryName:
        row = _mapping(value, frozenset({"id", "name"}))
        return cls(_uuid(row["id"]), _required(row["name"], str, "name"))


@dataclass(frozen=True, slots=True)
class PoolFacts:
    id: UUID
    name: str
    enabled: bool
    routing_policy: str
    legacy_instance_id: UUID | None
    routing_generation: int
    capability_fingerprint: str | None
    capability_snapshot: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, value: Any) -> PoolFacts:
        row = _mapping(value, frozenset(cls.__dataclass_fields__))
        return cls(
            id=_uuid(row["id"]),
            name=_required(row["name"], str, "pool name"),
            enabled=_required(row["enabled"], bool, "pool enabled"),
            routing_policy=_required(row["routing_policy"], str, "routing policy"),
            legacy_instance_id=_uuid(row["legacy_instance_id"], nullable=True),
            routing_generation=_required(
                row["routing_generation"], int, "routing generation"
            ),
            capability_fingerprint=_nullable(
                row["capability_fingerprint"], str, "capability fingerprint"
            ),
            capability_snapshot=_frozen_mapping(
                row["capability_snapshot"], "capability snapshot", nullable=True
            ),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class PoolMemberFacts:
    id: UUID
    pool_id: UUID
    registry_id: UUID
    traffic_state: str
    weight: int
    created_at: datetime
    updated_at: datetime
    registry: RegistryPublicFacts

    @classmethod
    def from_record(cls, value: Any) -> PoolMemberFacts:
        row = _mapping(value, frozenset(cls.__dataclass_fields__))
        return cls(
            id=_uuid(row["id"]),
            pool_id=_uuid(row["pool_id"]),
            registry_id=_uuid(row["registry_id"]),
            traffic_state=_required(row["traffic_state"], str, "member state"),
            weight=_required(row["weight"], int, "member weight"),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            registry=RegistryPublicFacts.from_record(row["registry"]),
        )


@dataclass(frozen=True, slots=True)
class ProjectAssociationFacts:
    id: UUID
    project_id: UUID
    pool_id: UUID
    enabled: bool
    default_variants: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, value: Any) -> ProjectAssociationFacts:
        row = _mapping(value, frozenset(cls.__dataclass_fields__))
        return cls(
            id=_uuid(row["id"]),
            project_id=_uuid(row["project_id"]),
            pool_id=_uuid(row["pool_id"]),
            enabled=_required(row["enabled"], bool, "association enabled"),
            default_variants=_frozen_mapping(
                row["default_variants"], "default variants", nullable=True
            ),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class ProjectBackendFacts:
    project_id: UUID
    registry: RegistryPublicFacts
    pool_id: UUID | None
    pool_legacy_instance_id: UUID | None
    member_id: UUID | None
    traffic_state: str | None
    weight: int | None
    association: ProjectAssociationFacts | None

    @classmethod
    def from_record(cls, value: Any) -> ProjectBackendFacts:
        row = _mapping(value, frozenset(cls.__dataclass_fields__))
        return cls(
            project_id=_uuid(row["project_id"]),
            registry=RegistryPublicFacts.from_record(row["registry"]),
            pool_id=_uuid(row["pool_id"], nullable=True),
            pool_legacy_instance_id=_uuid(
                row["pool_legacy_instance_id"], nullable=True
            ),
            member_id=_uuid(row["member_id"], nullable=True),
            traffic_state=_nullable(row["traffic_state"], str, "member state"),
            weight=_nullable(row["weight"], int, "member weight"),
            association=(
                ProjectAssociationFacts.from_record(row["association"])
                if row["association"] is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectPoolFacts:
    project_id: UUID
    pool: PoolFacts
    member_count: int
    association: ProjectAssociationFacts | None

    @classmethod
    def from_record(cls, value: Any) -> ProjectPoolFacts:
        row = _mapping(value, frozenset(cls.__dataclass_fields__))
        return cls(
            project_id=_uuid(row["project_id"]),
            pool=PoolFacts.from_record(row["pool"]),
            member_count=_required(row["member_count"], int, "member count"),
            association=(
                ProjectAssociationFacts.from_record(row["association"])
                if row["association"] is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CapabilityDriftPreview:
    pool_id: UUID
    registry_id: UUID
    member_state: str
    pool_enabled: bool
    pool_fingerprint: str | None
    candidate_fingerprint: str | None
    differing_fields: tuple[str, ...]
    has_drift: bool
    can_accept: bool
    blocking_member_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CapabilityDriftAcceptance:
    old_fingerprint: str
    new_fingerprint: str
    differing_fields: tuple[str, ...]
    pool_enabled: bool
