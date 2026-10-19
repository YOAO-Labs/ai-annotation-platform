"""Allowlisted asyncpg executor and SQL preparation for native capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Any

from pydantic import BaseModel

from aap_preannotation_results import contract as c

EXECUTOR_OPERATIONS = frozenset(
    name for name, operation in c.OPERATION.items() if operation.role == "executor"
)


def prepare_native_call(
    operation: str,
    request: BaseModel,
    *,
    writer: bool = False,
    allowed_roles: Collection[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Return one schema-qualified native call with one JSONB argument.

    The default is executor scope. Root's Labeling transaction bridge opts into
    writer operations with ``writer=True`` and still owns the transaction.
    """
    definition = c.OPERATION.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid preannotation result operation/request pair")
    roles = (
        frozenset(allowed_roles)
        if allowed_roles is not None
        else (frozenset({"executor", "writer"}) if writer else frozenset({"executor"}))
    )
    if definition.role not in roles:
        raise PermissionError("native operation is outside this database login scope")
    payload = c.json_bytes(request)
    if len(payload) > c.MAX_REQUEST_BYTES:
        raise c.BatchResultFailure("invalid_request")
    return (
        f"SELECT {definition.namespace}.{operation}($1::jsonb)",
        (payload.decode("utf-8"),),
    )


def validate_native_result(operation: str, request: BaseModel, raw: Any) -> BaseModel:
    try:
        return c.decode_reply(operation, request, raw)
    except c.BatchResultFailure:
        raise
    except (TypeError, ValueError):
        raise c.BatchResultFailure("invalid_response") from None


class AsyncpgBatchResultDatabase:
    """One committed transaction per executor operation; accepts no app objects."""

    def __init__(self, pool: Any, *, timeout_seconds: float = 5) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        self._pool = pool
        self._timeout = timeout_seconds

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        if operation not in EXECUTOR_OPERATIONS:
            raise c.BatchResultFailure("invalid_request")
        try:
            statement, arguments = prepare_native_call(operation, request)
        except c.BatchResultFailure:
            raise
        except (TypeError, ValueError, PermissionError):
            raise c.BatchResultFailure("invalid_request") from None

        try:
            async with self._pool.acquire(timeout=self._timeout) as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('statement_timeout', $1, true)",
                        str(int(self._timeout * 1000)),
                    )
                    raw = await connection.fetchval(
                        statement,
                        *arguments,
                        timeout=self._timeout,
                    )
                    # Validate the envelope and all scope/echo fields before the
                    # transaction context can commit.
                    result = validate_native_result(operation, request, raw)
            return result
        except asyncio.CancelledError:
            raise
        except c.BatchResultFailure:
            raise
        except Exception:
            raise c.BatchResultFailure("storage_unavailable") from None
