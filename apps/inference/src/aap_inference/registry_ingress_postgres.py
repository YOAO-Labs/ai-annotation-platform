"""Caller-owned asyncpg binding for Native Registry ingress operations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID, uuid4

from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
    RegistryEnvSyncStatusRequest,
    decode_registry_env_sync_status_result,
    registry_env_sync_policy_ready,
)

from .health_data_postgres import (
    BackendHealthRow,
    HealthDataUnavailable,
    HealthServicePoolRow,
    _decode_backend,
    _decode_pool,
    _jsonb,
    _record_values,
    _uuid,
)

_SCHEMA = "aap_inference_registry_ingress_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.registry_ingress_policy()"
_BIND_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id()::text"
_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"
_BY_URL_SQL = f"SELECT * FROM {_SCHEMA}.registry_by_url($1::text)"
_CANDIDATES_SQL = f"SELECT * FROM {_SCHEMA}.registry_candidates($1::text)"
_CREATE_REGISTRY_SQL = (
    f"SELECT * FROM {_SCHEMA}.create_registry("
    "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text)"
)
_SINGLETON_POOL_SQL = f"SELECT * FROM {_SCHEMA}.singleton_pool($1::uuid)"
_CREATE_SINGLETON_POOL_SQL = (
    f"SELECT * FROM {_SCHEMA}.create_singleton_pool("
    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text)"
)
_APPLY_OBSERVATION_SQL = (
    f"SELECT {_SCHEMA}.apply_observation("
    "$1::uuid,$2::text,$3::text,$4::text,$5::boolean,$6::text,"
    "$7::timestamptz,$8::text,$9::boolean,$10::text)"
)
_DISCONNECT_ENV_SQL = f"SELECT {_SCHEMA}.disconnect_env_registry($1::uuid,$2::text)"
_TAG_SEED_SQL = f"SELECT * FROM {_SCHEMA}.tag_seed_registry($1::uuid,$2::text)"
_SYNC_RESULT_SQL = f"SELECT {_SCHEMA}.sync_result($1::uuid,$2::text)"
_RECORD_SYNC_RESULT_SQL = (
    f"SELECT {_SCHEMA}.record_sync_result($1::uuid,$2::text,$3::integer)"
)

_FUNCTIONS = {
    ("registry_ingress_policy", ""),
    ("registry_by_url", "text"),
    ("registry_candidates", "text"),
    ("create_registry", "uuid, text, text, text, boolean, text"),
    ("singleton_pool", "uuid"),
    ("create_singleton_pool", "uuid, uuid, uuid, text, text, text"),
    (
        "apply_observation",
        "uuid, text, text, text, boolean, text, timestamp with time zone, text, boolean, text",
    ),
    ("disconnect_env_registry", "uuid, text"),
    ("tag_seed_registry", "uuid, text"),
    ("sync_result", "uuid, text"),
    ("record_sync_result", "uuid, text, integer"),
}


@dataclass
class _ObservationStage:
    row: BackendHealthRow
    profile: str
    state: str
    name: str
    is_interactive: bool
    health_meta: Any
    last_checked_at: Any
    error_message: str | None
    usable_caps: bool
    expected_snapshot: dict[str, Any]


@dataclass
class _DisconnectStage:
    row: BackendHealthRow
    expected_url: str


@dataclass
class _SeedMarkerStage:
    row: BackendHealthRow
    marker: dict[str, str]
    previous_extra_params: Any
    staged_extra_params: Any


class RegistryIngressPostgresData:
    """Borrow an active caller transaction for the fixed Registry ingress RPCs."""

    def __init__(
        self, connection: Any, transaction_id: str, *, autoflush: bool
    ) -> None:
        self._connection = connection
        self._transaction_id = transaction_id
        self.autoflush = autoflush
        self._backends: dict[UUID, BackendHealthRow] = {}
        self._pools: dict[UUID, HealthServicePoolRow] = {}
        self._pending: list[
            _ObservationStage | _DisconnectStage | _SeedMarkerStage
        ] = []

    @classmethod
    async def bind(
        cls,
        connection: Any,
        *,
        autoflush: bool = True,
    ) -> RegistryIngressPostgresData:
        if type(autoflush) is not bool:
            raise HealthDataUnavailable()
        try:
            if connection.is_in_transaction() is not True:
                raise HealthDataUnavailable()
            from aap_owner_data.postgres import verify_function_only_role

            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_FUNCTIONS,
            )
            policy = await connection.fetchval(_POLICY_SQL)
            if not registry_env_sync_policy_ready(policy):
                raise HealthDataUnavailable()
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
        except HealthDataUnavailable:
            raise
        except Exception:
            raise HealthDataUnavailable() from None
        if type(transaction_id) is not str or not transaction_id.isdecimal():
            raise HealthDataUnavailable()
        return cls(connection, transaction_id, autoflush=autoflush)

    async def _assert_transaction(self) -> None:
        try:
            if (
                self._connection.is_in_transaction() is not True
                or await self._connection.fetchval(_TRANSACTION_SQL)
                != self._transaction_id
            ):
                raise HealthDataUnavailable()
        except HealthDataUnavailable:
            raise
        except Exception:
            raise HealthDataUnavailable() from None

    async def _fetchrow(self, statement: str, *args):
        await self._assert_transaction()
        return await self._connection.fetchrow(statement, *args)

    async def _fetchval(self, statement: str, *args):
        await self._assert_transaction()
        return await self._connection.fetchval(statement, *args)

    async def _fetch(self, statement: str, *args):
        await self._assert_transaction()
        return await self._connection.fetch(statement, *args)

    async def _autoflush(self) -> None:
        if self.autoflush:
            await self.flush()

    def _remember_backend(self, row: BackendHealthRow) -> BackendHealthRow:
        existing = self._backends.get(row.id)
        if existing is None:
            self._backends[row.id] = row
            return row
        return existing

    def _remember_pool(self, row: HealthServicePoolRow) -> HealthServicePoolRow:
        existing = self._pools.get(row.id)
        if existing is None:
            self._pools[row.id] = row
            return row
        return existing

    async def by_url(self, url: str) -> BackendHealthRow | None:
        await self._autoflush()
        record = await self._fetchrow(_BY_URL_SQL, url)
        if record is None:
            return None
        return self._remember_backend(_decode_backend(record))

    async def candidates(self, profile: str) -> list[BackendHealthRow]:
        await self._autoflush()
        records = await self._fetch(_CANDIDATES_SQL, profile)
        return [self._remember_backend(_decode_backend(record)) for record in records]

    async def create_registry(
        self,
        name: str | None,
        url: str,
        source: str,
        *,
        is_interactive: bool = False,
        extra_params: Any = None,
    ) -> BackendHealthRow:
        # Legacy ORM insert flushes before the following singleton lookup.
        await self.flush()
        extra_json = json.dumps({} if extra_params is None else extra_params)
        record = await self._fetchrow(
            _CREATE_REGISTRY_SQL,
            uuid4(),
            name,
            url,
            source,
            is_interactive,
            extra_json,
        )
        if record is None:
            raise HealthDataUnavailable()
        return self._remember_backend(_decode_backend(record))

    async def singleton_pool(self, registry_id: UUID) -> HealthServicePoolRow | None:
        await self._autoflush()
        record = await self._fetchrow(_SINGLETON_POOL_SQL, _uuid(registry_id))
        if record is None:
            return None
        return self._remember_pool(_decode_pool(record))

    async def create_singleton_pool(
        self,
        row: BackendHealthRow,
        snapshot: dict[str, Any] | None,
        fingerprint: str | None,
    ) -> HealthServicePoolRow:
        # The legacy helper flushes the Pool row before inserting its Member.
        await self.flush()
        record = await self._fetchrow(
            _CREATE_SINGLETON_POOL_SQL,
            _uuid(row.id),
            uuid4(),
            uuid4(),
            row.name,
            json.dumps(snapshot),
            fingerprint,
        )
        if record is None:
            raise HealthDataUnavailable()
        return self._remember_pool(_decode_pool(record))

    async def stage_observation(
        self,
        row: BackendHealthRow,
        *,
        profile: str,
        usable_caps: bool,
        expected_snapshot: dict[str, Any],
    ) -> None:
        self._pending.append(
            _ObservationStage(
                row=row,
                profile=profile,
                state=row.state,
                name=row.name,
                is_interactive=row.is_interactive,
                health_meta=deepcopy(row.health_meta),
                last_checked_at=row.last_checked_at,
                error_message=row.error_message,
                usable_caps=usable_caps,
                expected_snapshot=deepcopy(expected_snapshot),
            )
        )

    async def stage_env_disconnect(self, row: BackendHealthRow) -> None:
        self._pending.append(_DisconnectStage(row=row, expected_url=row.url))

    async def stage_seed_marker(
        self,
        row: BackendHealthRow,
        marker: dict[str, str],
        previous_extra_params: Any,
    ) -> None:
        self._pending.append(
            _SeedMarkerStage(
                row=row,
                marker=deepcopy(marker),
                previous_extra_params=deepcopy(previous_extra_params),
                staged_extra_params=deepcopy(row.extra_params),
            )
        )

    async def _apply_observation(self, stage: _ObservationStage) -> None:
        checked_at = await self._fetchval(
            _APPLY_OBSERVATION_SQL,
            _uuid(stage.row.id),
            stage.profile,
            stage.state,
            stage.name,
            stage.is_interactive,
            json.dumps(stage.health_meta),
            stage.last_checked_at,
            stage.error_message,
            stage.usable_caps,
            json.dumps(stage.expected_snapshot, separators=(",", ":")),
        )
        stage.row.updated_at = checked_at

    async def _apply_disconnect(self, stage: _DisconnectStage) -> None:
        updated_at = await self._fetchval(
            _DISCONNECT_ENV_SQL,
            _uuid(stage.row.id),
            stage.expected_url,
        )
        stage.row.updated_at = updated_at

    async def _apply_seed_marker(self, stage: _SeedMarkerStage) -> None:
        record = await self._fetchrow(
            _TAG_SEED_SQL,
            _uuid(stage.row.id),
            json.dumps(stage.marker),
        )
        if record is None:
            raise HealthDataUnavailable()
        values = _record_values(record, ("extra_params", "updated_at"))
        merged_extra_params = _jsonb(values["extra_params"])
        stage.row.updated_at = values["updated_at"]
        if stage.row.extra_params == stage.staged_extra_params:
            stage.row.extra_params = merged_extra_params

    async def flush(self) -> None:
        while self._pending:
            stage = self._pending[0]
            if isinstance(stage, _ObservationStage):
                await self._apply_observation(stage)
            elif isinstance(stage, _DisconnectStage):
                await self._apply_disconnect(stage)
            else:
                await self._apply_seed_marker(stage)
            self._pending.pop(0)

    @staticmethod
    def _status_request(
        request_id: UUID,
        command_sha256: str,
    ) -> RegistryEnvSyncStatusRequest:
        return RegistryEnvSyncStatusRequest(
            schema=REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
            request_id=_uuid(request_id),
            command_sha256=command_sha256,
        )

    @classmethod
    def _receipt_result(
        cls,
        request_id: UUID,
        command_sha256: str,
        raw_result: Any,
    ) -> dict[str, Any]:
        status_request = cls._status_request(request_id, command_sha256)
        result = decode_registry_env_sync_status_result(status_request, raw_result)
        return result.model_dump(mode="json", by_alias=True)

    async def sync_result(
        self,
        request_id: UUID,
        command_sha256: str,
    ) -> dict[str, Any] | None:
        raw_result = await self._fetchval(
            _SYNC_RESULT_SQL,
            _uuid(request_id),
            command_sha256,
        )
        if raw_result is None:
            return None
        return self._receipt_result(request_id, command_sha256, raw_result)

    async def record_sync_result(
        self,
        request_id: UUID,
        command_sha256: str,
        observed_urls: int,
    ) -> dict[str, Any]:
        raw_result = await self._fetchval(
            _RECORD_SYNC_RESULT_SQL,
            _uuid(request_id),
            command_sha256,
            observed_urls,
        )
        if raw_result is None:
            raise HealthDataUnavailable()
        return self._receipt_result(request_id, command_sha256, raw_result)


__all__ = ["RegistryIngressPostgresData"]
