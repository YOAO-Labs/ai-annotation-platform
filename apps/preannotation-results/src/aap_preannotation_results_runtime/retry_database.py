"""Allowlisted asyncpg adapter for prediction retry native capabilities."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from aap_preannotation_results import retry_contract as c

EXECUTOR_OPERATIONS = frozenset(
    name for name, operation in c.OPERATION.items() if operation.role == "executor"
)
WRITER_OPERATIONS = frozenset(
    name for name, operation in c.OPERATION.items() if operation.role == "writer"
)


def prepare_native_call(
    operation: str,
    request: BaseModel,
    *,
    writer: bool = False,
) -> tuple[str, tuple[Any, ...]]:
    """Prepare one retry native call with one JSONB argument.

    Executor operations are available by default. Setting ``writer=True`` is an
    explicit business-transaction bridge capability and only enables admission
    and finalization operations.
    """
    definition = c.OPERATION.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid prediction retry operation/request pair")
    allowed = WRITER_OPERATIONS if writer else EXECUTOR_OPERATIONS
    if operation not in allowed:
        raise PermissionError("native operation is outside this database login scope")
    payload = c.json_bytes(request)
    if len(payload) > c.MAX_REQUEST_BYTES:
        raise c.PredictionRetryFailure("invalid_request")
    return (
        f"SELECT {definition.namespace}.{operation}($1::jsonb)",
        (payload.decode("utf-8"),),
    )


def validate_native_result(operation: str, request: BaseModel, raw: Any) -> BaseModel:
    try:
        return c.decode_reply(operation, request, raw)
    except c.PredictionRetryFailure:
        raise
    except (TypeError, ValueError):
        raise c.PredictionRetryFailure("invalid_response") from None


class AsyncpgPredictionRetryDatabase:
    """Executor-only adapter: each delivery/read operation commits separately."""

    def __init__(self, pool: Any, *, timeout_seconds: float = 5) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        self._pool = pool
        self._timeout = timeout_seconds

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        if operation not in EXECUTOR_OPERATIONS:
            raise c.PredictionRetryFailure("invalid_request")
        try:
            statement, arguments = prepare_native_call(operation, request)
        except c.PredictionRetryFailure:
            raise
        except (TypeError, ValueError, PermissionError):
            raise c.PredictionRetryFailure("invalid_request") from None

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
                    result = validate_native_result(operation, request, raw)
            return result
        except asyncio.CancelledError:
            raise
        except c.PredictionRetryFailure:
            raise
        except Exception:
            raise c.PredictionRetryFailure("storage_unavailable") from None
