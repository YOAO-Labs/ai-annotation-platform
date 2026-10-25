"""Caller-owned asyncpg binding for the fixed Inference routing RPCs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from aap_inference_contract.routing import (
    ROUTING_DATA_PROTOCOL,
    ROUTING_NATIVE_ACTIONS,
    ROUTING_REPLY_MAX_BYTES,
    ROUTING_REQUEST_MAX_BYTES,
    RoutingUnavailable,
    routing_native_policy_ready,
)
from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_ACTIONS,
    SCOPE_RETIREMENT_DATA_PROTOCOL,
    SCOPE_RETIREMENT_REPLY_MAX_BYTES,
    SCOPE_RETIREMENT_REQUEST_MAX_BYTES,
)
from aap_inference_contract.video_execution import (
    VIDEO_EXECUTION_SCOPE_ACTIONS,
    VideoScopeRejected,
    decode_video_scope_data,
)

_SCHEMA = "aap_inference_routing_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.routing_policy()"
_STEP_SQL = f"SELECT {_SCHEMA}.routing_step($1::text,$2::text)"
_RETIREMENT_STEP_SQL = f"SELECT {_SCHEMA}.scope_retirement_step($1::text,$2::text)"
_VIDEO_STEP_SQL = f"SELECT {_SCHEMA}.video_execution_scope_step($1::text,$2::text)"
_BIND_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id()::text"
_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"
_FUNCTIONS = {
    ("routing_policy", ""),
    ("routing_step", "text, text"),
    ("scope_retirement_step", "text, text"),
    ("video_execution_scope_step", "text, text"),
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _json_object(value: Any, *, max_bytes: int) -> dict[str, Any]:
    try:
        if type(value) in {str, bytes}:
            encoded = (
                value.encode("utf-8", errors="strict") if type(value) is str else value
            )
            if len(encoded) > max_bytes:
                raise RoutingUnavailable()
            parsed = json.loads(
                encoded,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        elif isinstance(value, Mapping):
            encoded = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > max_bytes:
                raise RoutingUnavailable()
            parsed = json.loads(
                encoded,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        else:
            raise RoutingUnavailable()
    except RoutingUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise RoutingUnavailable() from None
    if type(parsed) is not dict:
        raise RoutingUnavailable()
    return parsed


def _json_text(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = encoded.encode("utf-8", errors="strict")
        if len(raw) > ROUTING_REQUEST_MAX_BYTES:
            raise RoutingUnavailable()
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise RoutingUnavailable() from None
    return encoded


class RoutingPostgresData:
    """Bind the fixed Native routing step to one active caller transaction.

    The connection and transaction belong to the caller. Every step verifies that
    PostgreSQL still reports the transaction captured at bind time.
    """

    def __init__(self, connection: Any, transaction_id: str) -> None:
        self._connection = connection
        self._transaction_id = transaction_id

    @classmethod
    async def bind(cls, connection: Any) -> RoutingPostgresData:
        try:
            if connection.is_in_transaction() is not True:
                raise RoutingUnavailable()
            from aap_owner_data.postgres import verify_function_only_role

            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_FUNCTIONS,
            )
            policy = _json_object(
                await connection.fetchval(_POLICY_SQL),
                max_bytes=ROUTING_REPLY_MAX_BYTES,
            )
            if not routing_native_policy_ready(policy):
                raise RoutingUnavailable()
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
        except RoutingUnavailable:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise RoutingUnavailable() from None
        if type(transaction_id) is not str or not transaction_id.isdecimal():
            raise RoutingUnavailable()
        return cls(connection, transaction_id)

    async def _assert_transaction(self) -> None:
        try:
            if (
                self._connection.is_in_transaction() is not True
                or await self._connection.fetchval(_TRANSACTION_SQL)
                != self._transaction_id
            ):
                raise RoutingUnavailable()
        except RoutingUnavailable:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise RoutingUnavailable() from None

    async def step(self, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one closed action through ``routing_step(text,text)``."""
        if (
            type(action) is not str
            or action not in ROUTING_NATIVE_ACTIONS
            or not isinstance(arguments, Mapping)
            or any(type(key) is not str for key in arguments)
        ):
            raise RoutingUnavailable()
        request_json = _json_text(arguments)
        try:
            await self._assert_transaction()
            raw = await self._connection.fetchval(_STEP_SQL, action, request_json)
            reply = _json_object(raw, max_bytes=ROUTING_REPLY_MAX_BYTES)
        except RoutingUnavailable:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise RoutingUnavailable() from None
        if reply.get("schema") != ROUTING_DATA_PROTOCOL or reply.get("status") not in {
            "ok",
            "rejected",
        }:
            raise RoutingUnavailable()
        return reply

    async def retirement_step(
        self, action: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute one fixed scope-retirement RPC in the bound transaction."""
        if (
            type(action) is not str
            or action not in SCOPE_RETIREMENT_ACTIONS
            or not isinstance(arguments, Mapping)
            or any(type(key) is not str for key in arguments)
        ):
            raise RoutingUnavailable()
        request_json = _json_text(arguments)
        if len(request_json.encode("utf-8")) > SCOPE_RETIREMENT_REQUEST_MAX_BYTES:
            raise RoutingUnavailable()
        try:
            await self._assert_transaction()
            raw = await self._connection.fetchval(
                _RETIREMENT_STEP_SQL, action, request_json
            )
            reply = _json_object(raw, max_bytes=SCOPE_RETIREMENT_REPLY_MAX_BYTES)
        except RoutingUnavailable:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            raise RoutingUnavailable() from None
        if (
            reply.get("schema") != SCOPE_RETIREMENT_DATA_PROTOCOL
            or type(reply.get("status")) is not str
            or reply["status"] not in {"ok", "rejected"}
        ):
            raise RoutingUnavailable()
        payload = {key for key in reply if key not in {"schema", "status"}}
        if reply["status"] == "rejected":
            if (
                payload != {"reason"}
                or type(reply.get("reason")) is not str
                or reply["reason"]
                not in {
                    "scope_unavailable",
                    "backend_not_ready",
                    "transport_changed",
                    "management_busy",
                    "invalid_command",
                    "retirement_conflict",
                }
            ):
                raise RoutingUnavailable()
        elif payload != (
            {"ready", "functions"} if action == "policy" else {"retirement", "context"}
        ):
            raise RoutingUnavailable()
        return reply

    async def video_execution_scope_step(self, action, arguments):
        if (
            type(action) is not str
            or action not in VIDEO_EXECUTION_SCOPE_ACTIONS
            or not isinstance(arguments, Mapping)
            or any(type(key) is not str for key in arguments)
        ):
            raise RoutingUnavailable()
        request_json = _json_text(arguments)
        try:
            await self._assert_transaction()
            raw = await self._connection.fetchval(_VIDEO_STEP_SQL, action, request_json)
            reply = _json_object(raw, max_bytes=ROUTING_REPLY_MAX_BYTES)
            # Validation happens before the caller's transaction can commit.
            # Preserve the envelope for the manager's independent validation.
            try:
                decode_video_scope_data(action, dict(arguments), reply)
            except VideoScopeRejected:
                pass
            return reply
        except (
            RoutingUnavailable,
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ):
            raise
        except Exception:
            raise RoutingUnavailable() from None


__all__ = ["RoutingPostgresData"]
