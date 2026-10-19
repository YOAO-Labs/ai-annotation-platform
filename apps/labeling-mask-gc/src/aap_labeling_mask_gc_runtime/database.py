"""Allowlisted function-only PostgreSQL adapter for Labeling Mask authority."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Any, Protocol

from pydantic import BaseModel

from aap_labeling_mask_gc import contract as c

GC_OPERATIONS = frozenset(
    {
        "claim_mask_gc_sweep",
        "stage_mask_gc_page",
        "claim_mask_gc_deletes",
        "finish_mask_gc_delete",
        "read_mask_gc_progress",
    }
)


class MaskGcDatabase(Protocol):
    async def call(self, operation: str, request: BaseModel) -> BaseModel: ...


def prepare_native_call(
    operation: str,
    request: BaseModel,
    *,
    allowed_roles: Collection[str] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    definition = c.OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid Labeling Mask GC operation/request pair")
    if allowed_roles is not None and definition.role not in frozenset(allowed_roles):
        raise PermissionError("Mask GC operation is outside this SQL login scope")
    payload = c.json_bytes(request)
    if len(payload) > c.MAX_REQUEST_BYTES:
        raise c.MaskGcFailure("invalid_request")
    return (
        f"SELECT aap_labeling_v1.{operation}($1::jsonb)",
        (payload.decode("utf-8"),),
    )


def validate_native_result(operation: str, request: BaseModel, raw: Any) -> BaseModel:
    try:
        return c.decode_reply(operation, request, raw)
    except c.MaskGcFailure:
        raise
    except (TypeError, ValueError):
        raise c.MaskGcFailure("invalid_response") from None


class AsyncpgMaskGcData:
    """One committed transaction per native operation; accepts no app objects."""

    def __init__(
        self,
        pool: Any,
        *,
        timeout_seconds: float = 5,
        allowed_roles: Collection[str] = ("cleanup",),
    ) -> None:
        roles = frozenset(allowed_roles)
        if not roles or not roles <= {"cleanup"} or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid Labeling Mask GC database scope")
        self._pool = pool
        self._roles = roles
        self._timeout = timeout_seconds

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        if operation not in GC_OPERATIONS:
            raise c.MaskGcFailure("invalid_request")
        statement, arguments = prepare_native_call(
            operation, request, allowed_roles=self._roles
        )
        try:
            async with self._pool.acquire(timeout=self._timeout) as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('statement_timeout', $1, true)",
                        str(int(self._timeout * 1000)),
                    )
                    raw = await connection.fetchval(
                        statement, *arguments, timeout=self._timeout
                    )
                    result = validate_native_result(operation, request, raw)
            return result
        except asyncio.CancelledError:
            raise
        except c.MaskGcFailure:
            raise
        except (TypeError, ValueError, PermissionError):
            raise c.MaskGcFailure("invalid_request") from None
        except Exception:
            raise c.MaskGcFailure("storage_unavailable") from None

    async def check_ready(self) -> None:
        await self.call("read_mask_gc_progress", c.ReadMaskGcProgressRequest())
