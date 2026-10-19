"""Function-only, role-scoped PostgreSQL binding for the video input owner."""

from __future__ import annotations

import asyncio
from typing import Any, Collection

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs.values import Value

_ROLES = frozenset({"control", "worker", "labeling", "system"})


def prepare_native_call(
    operation: str, request: Value, *, allowed_roles: Collection[str]
) -> tuple[str, tuple[Any, ...]]:
    definition = c.OPERATIONS.get(operation)
    if definition is None or type(request) is not definition.request:
        raise TypeError("invalid video input operation/request pair")
    if definition.role not in allowed_roles:
        raise PermissionError("video input operation is outside this SQL login scope")
    document = request.model_dump(mode="json")
    if len(c.json_bytes(document)) > c.REQUEST_BYTES:
        raise c.VideoInputFailure("resource_budget_exceeded")
    arguments, placeholders = [], []
    for index, (name, kind) in enumerate(
        zip(definition.arguments, definition.sql_types, strict=True), start=1
    ):
        value = getattr(request, name)
        if value is not None and kind == "jsonb":
            value = c.json_bytes(document[name]).decode("utf-8")
        arguments.append(value)
        placeholders.append(f"${index}::{kind}")
    return f"SELECT aap_assets_v1.{operation}({', '.join(placeholders)})", tuple(
        arguments
    )


class AsyncpgVideoInputDatabase:
    def __init__(
        self, pool: Any, *, allowed_roles: Collection[str], timeout_seconds: float = 5
    ):
        roles = frozenset(allowed_roles)
        if not roles or not roles <= _ROLES or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid video input database role or timeout")
        self._pool, self._roles, self._timeout = pool, roles, timeout_seconds

    async def call(self, operation: str, request: Value) -> Value:
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
                    return c.parse_native(operation, request, raw)
        except asyncio.CancelledError:
            raise
        except c.VideoInputFailure:
            raise
        except Exception:
            raise c.VideoInputFailure("storage_unavailable") from None
