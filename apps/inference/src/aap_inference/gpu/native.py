"""Borrowed-transaction clients for the fixed GPU Native capabilities."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal, Protocol

from aap_inference_contract.gpu import (
    GPU_AUTHORITY_DATA_PROTOCOL,
    GPU_AUTHORITY_NATIVE_ACTIONS,
    GPU_AUTHORITY_REPLY_MAX_BYTES,
    GPU_AUTHORITY_REQUEST_MAX_BYTES,
    GPU_AUTHORITY_SCHEMA,
    GPU_CALLER_DATA_PROTOCOL,
    GPU_CALLER_NATIVE_ACTIONS,
    GPU_CALLER_REPLY_MAX_BYTES,
    GPU_CALLER_REQUEST_MAX_BYTES,
    GPU_COLLECTOR_DATA_PROTOCOL,
    GPU_COLLECTOR_NATIVE_ACTIONS,
    GPU_COLLECTOR_REPLY_MAX_BYTES,
    GPU_COLLECTOR_REQUEST_MAX_BYTES,
    GPU_COLLECTOR_SCHEMA,
    GPUAuthorityAction,
    GPUAuthorityUnavailable,
    GPUCallerAction,
    gpu_authority_native_policy_ready,
    gpu_caller_native_policy_ready,
    gpu_collector_native_policy_ready,
)


class GPUAuthorityData(Protocol):
    """Fixed operation port implemented by the dedicated GPU Native owner."""

    async def step(
        self, action: GPUAuthorityAction, arguments: Mapping[str, Any]
    ) -> dict[str, Any]: ...


class GPUCallerData(Protocol):
    """Finite caller-administration Native port without private authority actions."""

    async def step(
        self, action: GPUCallerAction, arguments: Mapping[str, Any]
    ) -> dict[str, Any]: ...


class GPUAuthorityDataProvider(GPUAuthorityData, Protocol):
    """Owner pool port offering one borrowed transaction at a time."""

    def transaction(self) -> AbstractAsyncContextManager[GPUAuthorityData]: ...


class GPUCollectorData(Protocol):
    """One-purpose collector Native capability; cannot use the authority profile."""

    async def step(
        self, action: Literal["collect_tombstone"], arguments: Mapping[str, Any]
    ) -> dict[str, Any]: ...


class GPUCollectorDataProvider(GPUCollectorData, Protocol):
    """Dedicated collector pool with the single, receipt-guarded GC action."""

    def transaction(self) -> AbstractAsyncContextManager[GPUCollectorData]: ...


_AUTHORITY_POLICY_SQL = "SELECT aap_inference_gpu_v1.gpu_authority_policy()"
_AUTHORITY_STEP_SQL = (
    "SELECT aap_inference_gpu_v1.gpu_authority_step($1::text,$2::text)"
)
_COLLECTOR_POLICY_SQL = "SELECT aap_inference_gpu_collector_v1.gpu_collector_policy()"
_COLLECTOR_STEP_SQL = (
    "SELECT aap_inference_gpu_collector_v1.gpu_collector_step($1::text,$2::text)"
)
_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"
_BIND_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id()::text"
_AUTHORITY_FUNCTIONS = {
    ("gpu_authority_policy", ""),
    ("gpu_authority_step", "text, text"),
}
_COLLECTOR_FUNCTIONS = {
    ("gpu_collector_policy", ""),
    ("gpu_collector_step", "text, text"),
}
_CALLER_POLICY_SQL = "SELECT aap_inference_gpu_caller_v1.gpu_caller_policy()"
_CALLER_STEP_SQL = (
    "SELECT aap_inference_gpu_caller_v1.gpu_caller_step($1::text,$2::text)"
)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode_object(value: Any, *, maximum: int) -> dict[str, Any]:
    try:
        if type(value) in {str, bytes}:
            raw = (
                value.encode("utf-8", errors="strict") if type(value) is str else value
            )
            if len(raw) > maximum:
                raise GPUAuthorityUnavailable()
            result = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        elif isinstance(value, Mapping):
            raw = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
            if len(raw) > maximum:
                raise GPUAuthorityUnavailable()
            result = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        else:
            raise GPUAuthorityUnavailable()
    except GPUAuthorityUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise GPUAuthorityUnavailable() from None
    if type(result) is not dict:
        raise GPUAuthorityUnavailable()
    return result


def _encode_arguments(arguments: Mapping[str, Any], *, maximum: int) -> str:
    if not isinstance(arguments, Mapping) or any(
        type(key) is not str for key in arguments
    ):
        raise GPUAuthorityUnavailable()
    try:
        encoded = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8", errors="strict")) > maximum:
            raise GPUAuthorityUnavailable()
    except GPUAuthorityUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise GPUAuthorityUnavailable() from None
    return encoded


class _BorrowedGPUData:
    __slots__ = (
        "_connection",
        "_transaction_id",
        "_actions",
        "_protocol",
        "_step_sql",
        "_max_request",
        "_max_reply",
    )

    def __init__(
        self,
        connection: Any,
        transaction_id: str,
        *,
        actions: tuple[str, ...],
        protocol: str,
        step_sql: str,
        max_request: int,
        max_reply: int,
    ) -> None:
        self._connection = connection
        self._transaction_id = transaction_id
        self._actions = frozenset(actions)
        self._protocol = protocol
        self._step_sql = step_sql
        self._max_request = max_request
        self._max_reply = max_reply

    async def _assert_transaction(self) -> None:
        try:
            if (
                self._connection.is_in_transaction() is not True
                or await self._connection.fetchval(_TRANSACTION_SQL)
                != self._transaction_id
            ):
                raise GPUAuthorityUnavailable()
        except GPUAuthorityUnavailable:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise GPUAuthorityUnavailable() from None

    async def step(self, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if type(action) is not str or action not in self._actions:
            raise GPUAuthorityUnavailable()
        request_json = _encode_arguments(arguments, maximum=self._max_request)
        try:
            await self._assert_transaction()
            raw = await self._connection.fetchval(self._step_sql, action, request_json)
            reply = _decode_object(raw, maximum=self._max_reply)
        except GPUAuthorityUnavailable:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise GPUAuthorityUnavailable() from None
        if reply.get("protocol") != self._protocol:
            raise GPUAuthorityUnavailable()
        if reply.get("action") != action or type(reply.get("result")) is not dict:
            raise GPUAuthorityUnavailable()
        return reply["result"]


class GPUAuthorityPostgresData(_BorrowedGPUData):
    """Function-only authority RPC bound to a caller-owned PostgreSQL transaction."""

    @classmethod
    async def bind(cls, connection: Any) -> GPUAuthorityPostgresData:
        try:
            if connection.is_in_transaction() is not True:
                raise GPUAuthorityUnavailable()
            from aap_owner_data.postgres import verify_function_only_role

            await verify_function_only_role(
                connection,
                schema=GPU_AUTHORITY_SCHEMA,
                functions=_AUTHORITY_FUNCTIONS,
            )
            policy = _decode_object(
                await connection.fetchval(_AUTHORITY_POLICY_SQL),
                maximum=GPU_AUTHORITY_REPLY_MAX_BYTES,
            )
            if not gpu_authority_native_policy_ready(policy):
                raise GPUAuthorityUnavailable()
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
        except GPUAuthorityUnavailable:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise GPUAuthorityUnavailable() from None
        if type(transaction_id) is not str or not transaction_id.isdecimal():
            raise GPUAuthorityUnavailable()
        return cls(
            connection,
            transaction_id,
            protocol=GPU_AUTHORITY_DATA_PROTOCOL,
            actions=GPU_AUTHORITY_NATIVE_ACTIONS,
            step_sql=_AUTHORITY_STEP_SQL,
            max_request=GPU_AUTHORITY_REQUEST_MAX_BYTES,
            max_reply=GPU_AUTHORITY_REPLY_MAX_BYTES,
        )


class GPUCallerPostgresData(_BorrowedGPUData):
    """Borrowed caller-UOW profile limited to safe administrative facts/actions."""

    @classmethod
    async def bind(cls, connection: Any) -> GPUCallerPostgresData:
        try:
            if connection.is_in_transaction() is not True:
                raise GPUAuthorityUnavailable()
            policy = _decode_object(
                await connection.fetchval(_CALLER_POLICY_SQL),
                maximum=GPU_CALLER_REPLY_MAX_BYTES,
            )
            if not gpu_caller_native_policy_ready(policy):
                raise GPUAuthorityUnavailable()
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
        except GPUAuthorityUnavailable:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise GPUAuthorityUnavailable() from None
        if type(transaction_id) is not str or not transaction_id.isdecimal():
            raise GPUAuthorityUnavailable()
        return cls(
            connection,
            transaction_id,
            protocol=GPU_CALLER_DATA_PROTOCOL,
            actions=GPU_CALLER_NATIVE_ACTIONS,
            step_sql=_CALLER_STEP_SQL,
            max_request=GPU_CALLER_REQUEST_MAX_BYTES,
            max_reply=GPU_CALLER_REPLY_MAX_BYTES,
        )


class GPUAuthorityPoolData:
    """Open one short caller-owned transaction for each runtime Native action."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[GPUAuthorityData]:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                yield await GPUAuthorityPostgresData.bind(connection)

    async def step(
        self,
        action: GPUAuthorityAction,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        async with self.transaction() as data:
            return await data.step(action, arguments)


class GPUCollectorPostgresData(_BorrowedGPUData):
    """Separate function-only collector profile for the tombstone-GC capability."""

    @classmethod
    async def bind(cls, connection: Any) -> GPUCollectorPostgresData:
        try:
            if connection.is_in_transaction() is not True:
                raise GPUAuthorityUnavailable()
            from aap_owner_data.postgres import verify_function_only_role

            await verify_function_only_role(
                connection,
                schema=GPU_COLLECTOR_SCHEMA,
                functions=_COLLECTOR_FUNCTIONS,
            )
            policy = _decode_object(
                await connection.fetchval(_COLLECTOR_POLICY_SQL),
                maximum=GPU_COLLECTOR_REPLY_MAX_BYTES,
            )
            if not gpu_collector_native_policy_ready(policy):
                raise GPUAuthorityUnavailable()
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
        except GPUAuthorityUnavailable:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise GPUAuthorityUnavailable() from None
        if type(transaction_id) is not str or not transaction_id.isdecimal():
            raise GPUAuthorityUnavailable()
        return cls(
            connection,
            transaction_id,
            protocol=GPU_COLLECTOR_DATA_PROTOCOL,
            actions=GPU_COLLECTOR_NATIVE_ACTIONS,
            step_sql=_COLLECTOR_STEP_SQL,
            max_request=GPU_COLLECTOR_REQUEST_MAX_BYTES,
            max_reply=GPU_COLLECTOR_REPLY_MAX_BYTES,
        )


__all__ = [
    "GPUAuthorityData",
    "GPUAuthorityDataProvider",
    "GPUAuthorityPoolData",
    "GPUAuthorityPostgresData",
    "GPUCollectorData",
    "GPUCollectorDataProvider",
    "GPUCallerData",
    "GPUCallerPostgresData",
    "GPUCollectorPostgresData",
]
