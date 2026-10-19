"""Function-only PostgreSQL binding for the accepted-input owner."""

from __future__ import annotations

import asyncio
from typing import Any, Collection, Protocol

from pydantic import BaseModel

from aap_assets_inputs import contract as c

_ROLES = frozenset({"control", "worker", "labeling", "system", "cleanup"})


class InputDatabase(Protocol):
    async def call(self, operation: str, request: BaseModel) -> BaseModel: ...


def prepare_native_call(
    operation: str,
    request: BaseModel,
    *,
    allowed_roles: Collection[str],
) -> tuple[str, tuple[Any, ...]]:
    definition = c.OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid input operation/request pair")
    if definition.role not in allowed_roles:
        raise PermissionError("input operation is outside this SQL login scope")
    if len(c.json_bytes(request)) > c.REQUEST_BYTES:
        raise c.InputFailure("resource_budget_exceeded")
    arguments, placeholders = [], []
    for index, (name, kind) in enumerate(
        zip(definition.arguments, definition.sql_types, strict=True), start=1
    ):
        value = getattr(request, name)
        if value is not None and kind == "jsonb":
            value = c.json_bytes(value).decode("utf-8")
        arguments.append(value)
        placeholders.append(f"${index}::{kind}")
    return f"SELECT aap_assets_v1.{operation}({', '.join(placeholders)})", tuple(
        arguments
    )


def validate_native_result(operation: str, request: BaseModel, raw: Any) -> BaseModel:
    definition = c.OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid input operation/request pair")
    result = c.decode_reply(raw, definition.result)
    try:
        c.require_scope(request, result)
    except (ValueError, TypeError, AttributeError):
        raise c.InputFailure("invalid_response") from None
    return result


class AsyncpgInputDatabase:
    def __init__(
        self, pool: Any, *, allowed_roles: Collection[str], timeout_seconds: float = 5
    ):
        roles = frozenset(allowed_roles)
        if not roles or not roles <= _ROLES or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid input database role or timeout")
        self._pool = pool
        self._roles = roles
        self._timeout = timeout_seconds

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        statement, arguments = prepare_native_call(
            operation, request, allowed_roles=self._roles
        )
        try:
            async with self._pool.acquire(timeout=self._timeout) as db:
                async with db.transaction():
                    await db.execute(
                        "SELECT set_config('statement_timeout', $1, true)",
                        str(int(self._timeout * 1000)),
                    )
                    raw = await db.fetchval(
                        statement, *arguments, timeout=self._timeout
                    )
                    return validate_native_result(operation, request, raw)
        except asyncio.CancelledError:
            raise
        except c.InputFailure:
            raise
        except Exception:
            raise c.InputFailure("storage_unavailable") from None
