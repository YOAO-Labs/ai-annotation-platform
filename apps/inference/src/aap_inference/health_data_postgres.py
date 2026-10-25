"""Caller-owned asyncpg binding for Native Inference health data functions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

_SCHEMA = "aap_inference_health_data_v1"
_POLICY_SCHEMA = "aap-inference-health-data-native/v1"
_DATA_PROTOCOL = "aap-inference-health-data/v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.health_data_policy()"
_BIND_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id()::text"
_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"
_BACKEND_READ_SQL = f"SELECT * FROM {_SCHEMA}.backend_row($1::uuid, false)"
_BACKEND_LOCK_SQL = f"SELECT * FROM {_SCHEMA}.backend_row($1::uuid, true)"
_MEMBERSHIP_READ_SQL = (
    f"SELECT * FROM {_SCHEMA}.membership_row($1::uuid, $2::text, false)"
)
_MEMBERSHIP_LOCK_SQL = (
    f"SELECT * FROM {_SCHEMA}.membership_row($1::uuid, $2::text, true)"
)
_CLOCK_SQL = f"SELECT {_SCHEMA}.observation_clock()"
_LOCK_RESOURCE_SQL = f"SELECT {_SCHEMA}.lock_resource($1::text)"
_TRY_PROMOTION_SQL = f"SELECT {_SCHEMA}.try_promotion()"
_WRITE_BACKEND_SQL = (
    f"SELECT {_SCHEMA}.write_backend("
    "$1::uuid, $2::text, $3::timestamptz, $4::text, $5::boolean, $6::boolean)"
)
_MEMBER_POOL_SQL = f"SELECT {_SCHEMA}.member_pool($1::uuid)"
_POOL_SQL = f"SELECT * FROM {_SCHEMA}.pool_row($1::uuid, $2::uuid)"
_MEMBER_SQL = f"SELECT * FROM {_SCHEMA}.member_row($1::uuid)"
_APPLY_RECONCILIATION_SQL = (
    f"SELECT * FROM {_SCHEMA}.apply_reconciliation("
    "$1::uuid, $2::uuid, $3::text, $4::text, $5::text, $6::bigint, "
    "$7::text, $8::text)"
)

_BACKEND_FIELDS = (
    "id",
    "name",
    "url",
    "state",
    "is_interactive",
    "auth_method",
    "auth_token",
    "extra_params",
    "gpu_resource_id",
    "vram_budget_mb",
    "eviction_priority",
    "health_meta",
    "source",
    "error_message",
    "last_checked_at",
    "created_at",
    "updated_at",
)
_POOL_FIELDS = (
    "id",
    "name",
    "enabled",
    "routing_policy",
    "legacy_instance_id",
    "routing_generation",
    "capability_fingerprint",
    "capability_snapshot",
    "created_at",
    "updated_at",
)
_MEMBER_FIELDS = (
    "id",
    "pool_id",
    "registry_id",
    "traffic_state",
    "weight",
    "created_at",
    "updated_at",
)
_MEMBERSHIP_FIELDS = ("membership_epoch", "state")
_POLICY_FUNCTIONS = (
    f"{_SCHEMA}.health_data_policy()",
    f"{_SCHEMA}.backend_row(uuid,boolean)",
    f"{_SCHEMA}.membership_row(uuid,text,boolean)",
    f"{_SCHEMA}.observation_clock()",
    f"{_SCHEMA}.lock_resource(text)",
    f"{_SCHEMA}.try_promotion()",
    f"{_SCHEMA}.write_backend(uuid,text,timestamptz,text,boolean,boolean)",
    f"{_SCHEMA}.member_pool(uuid)",
    f"{_SCHEMA}.pool_row(uuid,uuid)",
    f"{_SCHEMA}.member_row(uuid)",
    f"{_SCHEMA}.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)",
)
_FUNCTIONS = {
    ("health_data_policy", ""),
    ("backend_row", "uuid, boolean"),
    ("membership_row", "uuid, text, boolean"),
    ("observation_clock", ""),
    ("lock_resource", "text"),
    ("try_promotion", ""),
    ("write_backend", "uuid, text, timestamp with time zone, text, boolean, boolean"),
    ("member_pool", "uuid"),
    ("pool_row", "uuid, uuid"),
    ("member_row", "uuid"),
    (
        "apply_reconciliation",
        "uuid, uuid, text, text, text, bigint, text, text",
    ),
}


class HealthDataUnavailable(RuntimeError):
    """A malformed Native value or an incompatible owner policy was returned."""

    def __init__(self) -> None:
        super().__init__("Inference health data unavailable")


@dataclass
class BackendHealthRow:
    id: UUID
    name: str
    url: str
    state: str
    is_interactive: bool
    auth_method: str | None
    auth_token: str | None = field(repr=False)
    extra_params: Any
    gpu_resource_id: str | None
    vram_budget_mb: int | None
    eviction_priority: int
    health_meta: Any
    source: str
    error_message: str | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class MembershipHealthRow:
    membership_epoch: int
    state: str


@dataclass
class HealthPoolMemberRow:
    id: UUID
    pool_id: UUID
    registry_id: UUID
    traffic_state: str
    weight: int
    created_at: datetime
    updated_at: datetime


@dataclass
class HealthServicePoolRow:
    id: UUID
    name: str
    enabled: bool
    routing_policy: str
    legacy_instance_id: UUID | None
    routing_generation: int
    capability_fingerprint: str | None
    capability_snapshot: Any
    created_at: datetime
    updated_at: datetime


@dataclass
class _ReconciliationIntent:
    registry_id: UUID
    pool: HealthServicePoolRow
    member: HealthPoolMemberRow
    operation: Literal["seed", "disable"]
    snapshot: dict[str, Any]
    fingerprint: str
    expected_generation: int
    expected_pool_fingerprint: str | None
    expected_member_state: str


def health_data_policy_ready(value: object) -> bool:
    """Return true only for the exact eleven-function Native policy."""
    if type(value) is bytes:
        try:
            value = value.decode("utf-8")
        except UnicodeError:
            return False
    if type(value) is str:
        try:
            value = json.loads(value)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False
    if type(value) is not dict:
        return False
    return bool(
        set(value) == {"schema", "data_protocol", "transaction", "functions", "ready"}
        and type(value["schema"]) is str
        and value["schema"] == _POLICY_SCHEMA
        and type(value["data_protocol"]) is str
        and value["data_protocol"] == _DATA_PROTOCOL
        and type(value["transaction"]) is str
        and value["transaction"] == "caller_owned"
        and type(value["functions"]) is list
        and all(type(name) is str for name in value["functions"])
        and value["functions"] == list(_POLICY_FUNCTIONS)
        and type(value["ready"]) is bool
        and value["ready"] is True
    )


def _uuid(value: Any, *, nullable: bool = False) -> UUID | None:
    if value is None and nullable:
        return None
    try:
        if isinstance(value, UUID):
            return UUID(bytes=value.bytes)
        if type(value) is str:
            return UUID(value)
    except (AttributeError, ValueError, TypeError):
        pass
    raise HealthDataUnavailable()


def _jsonb(value: Any) -> Any:
    if type(value) in {str, bytes}:
        # asyncpg returns JSON/JSONB in its wire text form unless a codec has
        # been registered; decode every field from the full Native row.
        return json.loads(value)
    return value


def _record_values(record: Any, expected_fields: tuple[str, ...]) -> dict[str, Any]:
    try:
        values = {name: record[name] for name in expected_fields}
        keys = set(record.keys())
    except (AttributeError, KeyError, TypeError):
        raise HealthDataUnavailable() from None
    if keys != set(expected_fields):
        raise HealthDataUnavailable()
    return values


def _decode_backend(record: Any) -> BackendHealthRow:
    values = _record_values(record, _BACKEND_FIELDS)
    values["id"] = _uuid(values["id"])
    values["extra_params"] = _jsonb(values["extra_params"])
    values["health_meta"] = _jsonb(values["health_meta"])
    return BackendHealthRow(**values)


def _decode_membership(record: Any) -> MembershipHealthRow:
    values = _record_values(record, _MEMBERSHIP_FIELDS)
    return MembershipHealthRow(**values)


def _decode_pool(record: Any) -> HealthServicePoolRow:
    values = _record_values(record, _POOL_FIELDS)
    values["id"] = _uuid(values["id"])
    values["legacy_instance_id"] = _uuid(values["legacy_instance_id"], nullable=True)
    values["capability_snapshot"] = _jsonb(values["capability_snapshot"])
    return HealthServicePoolRow(**values)


def _decode_member(record: Any) -> HealthPoolMemberRow:
    values = _record_values(record, _MEMBER_FIELDS)
    for field_name in ("id", "pool_id", "registry_id"):
        values[field_name] = _uuid(values[field_name])
    return HealthPoolMemberRow(**values)


def _copy_row(target: Any, source: Any) -> None:
    for descriptor in fields(source):
        setattr(target, descriptor.name, getattr(source, descriptor.name))


def _canonical_snapshot_json(snapshot: dict[str, Any]) -> str:
    # Match canonical capability fingerprint ordering and preserve the old JSONB
    # serialization phase for NaN/Infinity at the deferred Native flush.
    return json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class HealthDataPostgresData:
    """Borrow one active transaction; discard this binding when the caller ends it."""

    def __init__(self, connection: Any, transaction_id: str) -> None:
        self._connection = connection
        self._transaction_id = transaction_id
        self._backends: dict[UUID, BackendHealthRow] = {}
        self._pools: dict[UUID, HealthServicePoolRow] = {}
        self._members: dict[UUID, HealthPoolMemberRow] = {}
        self._intent: _ReconciliationIntent | None = None

    @classmethod
    async def bind(cls, connection: Any) -> HealthDataPostgresData:
        try:
            active = connection.is_in_transaction()
        except (AttributeError, TypeError, ValueError):
            raise HealthDataUnavailable() from None
        if active is not True:
            raise HealthDataUnavailable()
        try:
            from aap_owner_data.postgres import (
                verify_function_only_role,
            )

            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_FUNCTIONS,
            )
            policy = await connection.fetchval(_POLICY_SQL)
        except Exception:
            raise HealthDataUnavailable() from None
        if not health_data_policy_ready(policy):
            raise HealthDataUnavailable()
        try:
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
        except Exception:
            raise HealthDataUnavailable() from None
        if type(transaction_id) is not str or not transaction_id.isdecimal():
            raise HealthDataUnavailable()
        return cls(connection, transaction_id)

    async def _assert_transaction(self) -> None:
        if (
            self._connection.is_in_transaction() is not True
            or await self._connection.fetchval(_TRANSACTION_SQL) != self._transaction_id
        ):
            raise HealthDataUnavailable()

    async def _fetchrow(self, statement, *args):
        await self._assert_transaction()
        return await self._connection.fetchrow(statement, *args)

    async def _fetchval(self, statement, *args):
        await self._assert_transaction()
        return await self._connection.fetchval(statement, *args)

    async def _backend_row(self, registry_id: UUID, *, for_update: bool):
        statement = _BACKEND_LOCK_SQL if for_update else _BACKEND_READ_SQL
        record = await self._fetchrow(statement, _uuid(registry_id))
        if record is None:
            return None
        return _decode_backend(record)

    def _remember_backend(self, row: BackendHealthRow) -> BackendHealthRow:
        existing = self._backends.get(row.id)
        if existing is None:
            self._backends[row.id] = row
            return row
        _copy_row(existing, row)
        return existing

    async def get_backend(self, registry_id: UUID) -> BackendHealthRow | None:
        backend_id = _uuid(registry_id)
        row = await self._backend_row(backend_id, for_update=False)
        if row is None:
            return None
        # ORM SELECT materializes the full row even when identity-map values win.
        cached = self._backends.get(backend_id)
        if cached is not None:
            return cached
        return self._remember_backend(row)

    async def read_membership(
        self,
        registry_id: UUID,
        gpu_resource_id: str,
        *,
        for_update: bool = False,
    ) -> MembershipHealthRow | None:
        if type(gpu_resource_id) is not str:
            raise HealthDataUnavailable()
        statement = _MEMBERSHIP_LOCK_SQL if for_update else _MEMBERSHIP_READ_SQL
        record = await self._fetchrow(
            statement,
            _uuid(registry_id),
            gpu_resource_id,
        )
        return None if record is None else _decode_membership(record)

    async def clock_timestamp(self) -> datetime | None:
        return await self._fetchval(_CLOCK_SQL)

    async def lock_current_backend(self, registry_id: UUID) -> BackendHealthRow | None:
        row = await self._backend_row(registry_id, for_update=True)
        if row is None:
            return None
        return self._remember_backend(row)

    async def lock_gpu_resource(self, resource_id: str) -> None:
        if type(resource_id) is not str:
            raise HealthDataUnavailable()
        await self._fetchval(_LOCK_RESOURCE_SQL, resource_id)

    async def try_membership_promotion(self) -> bool | None:
        return await self._fetchval(_TRY_PROMOTION_SQL)

    async def write_health(
        self,
        registry_id: UUID,
        values: dict[str, Any],
    ) -> int | None:
        allowed = {"state", "last_checked_at", "health_meta", "is_interactive"}
        if (
            type(values) is not dict
            or not {"state", "last_checked_at", "health_meta"} <= set(values)
            or not set(values) <= allowed
        ):
            raise HealthDataUnavailable()
        state = values["state"]
        observed_at = values["last_checked_at"]
        # SQLAlchemy's JSONB serializer encodes Python None as JSON null by default.
        health_meta_json = json.dumps(values["health_meta"])
        has_interactive = "is_interactive" in values
        interactive = values["is_interactive"] if has_interactive else False
        rowcount = await self._fetchval(
            _WRITE_BACKEND_SQL,
            _uuid(registry_id),
            state,
            observed_at,
            health_meta_json,
            interactive,
            has_interactive,
        )
        if rowcount is None:
            return None
        return rowcount

    async def refresh_backend(self, backend: BackendHealthRow) -> None:
        registry_id = _uuid(backend.id)
        row = await self._backend_row(registry_id, for_update=False)
        if row is None:
            raise HealthDataUnavailable()
        cached = self._backends.get(registry_id)
        if cached is None:
            cached = backend
            self._backends[registry_id] = cached
        _copy_row(cached, row)
        if backend is not cached:
            _copy_row(backend, cached)

    async def read_member_pool(self, registry_id: UUID) -> UUID | None:
        value = await self._fetchval(_MEMBER_POOL_SQL, _uuid(registry_id))
        return _uuid(value, nullable=True)

    async def load_pool_member(self, registry_id: UUID) -> HealthPoolMemberRow | None:
        registry_id = _uuid(registry_id)
        record = await self._fetchrow(_MEMBER_SQL, registry_id)
        row = None if record is None else _decode_member(record)
        if self._intent is not None and self._intent.member.registry_id == registry_id:
            self._intent = None
        if row is None:
            return None
        existing = self._members.get(row.registry_id)
        if existing is None:
            self._members[row.registry_id] = row
            return row
        _copy_row(existing, row)
        return existing

    async def load_pool(
        self,
        pool_id: UUID,
        *,
        registry_id: UUID,
    ) -> HealthServicePoolRow | None:
        pool_id = _uuid(pool_id)
        record = await self._fetchrow(
            _POOL_SQL,
            pool_id,
            _uuid(registry_id),
        )
        row = None if record is None else _decode_pool(record)
        if self._intent is not None and self._intent.pool.id == pool_id:
            self._intent = None
        if row is None:
            return None
        existing = self._pools.get(row.id)
        if existing is None:
            self._pools[row.id] = row
            return row
        _copy_row(existing, row)
        return existing

    async def stage_pool_reconciliation(
        self,
        registry_id: UUID,
        pool: HealthServicePoolRow,
        member: HealthPoolMemberRow,
        *,
        operation: Literal["seed", "disable"],
        snapshot: dict[str, Any],
        fingerprint: str,
        expected_generation: int,
        expected_pool_fingerprint: str | None,
        expected_member_state: str,
    ) -> None:
        if self._intent is not None:
            raise HealthDataUnavailable()
        if type(operation) is not str or operation not in {"seed", "disable"}:
            raise HealthDataUnavailable()
        self._intent = _ReconciliationIntent(
            registry_id=_uuid(registry_id),
            pool=pool,
            member=member,
            operation=operation,
            snapshot=deepcopy(snapshot),
            fingerprint=fingerprint,
            expected_generation=expected_generation,
            expected_pool_fingerprint=expected_pool_fingerprint,
            expected_member_state=expected_member_state,
        )

    async def flush(self) -> None:
        intent = self._intent
        if intent is None:
            return
        snapshot_json = _canonical_snapshot_json(intent.snapshot)
        record = await self._fetchrow(
            _APPLY_RECONCILIATION_SQL,
            intent.registry_id,
            _uuid(intent.pool.id),
            intent.operation,
            snapshot_json,
            intent.fingerprint,
            intent.expected_generation,
            intent.expected_pool_fingerprint,
            intent.expected_member_state,
        )
        if record is None:
            raise HealthDataUnavailable()
        self._intent = None
        values = _record_values(record, ("pool_updated_at", "member_updated_at"))
        pool_updated_at = values["pool_updated_at"]
        member_updated_at = values["member_updated_at"]
        if (intent.operation == "seed") != (member_updated_at is None):
            raise HealthDataUnavailable()
        intent.pool.updated_at = pool_updated_at
        if member_updated_at is not None:
            intent.member.updated_at = member_updated_at
        self._pools[intent.pool.id] = intent.pool
        self._members[intent.member.registry_id] = intent.member


__all__ = [
    "BackendHealthRow",
    "HealthDataPostgresData",
    "HealthDataUnavailable",
    "HealthPoolMemberRow",
    "HealthServicePoolRow",
    "MembershipHealthRow",
    "health_data_policy_ready",
]
