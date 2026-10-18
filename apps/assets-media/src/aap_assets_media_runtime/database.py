"""Transactional bindings for the frozen Assets media SQL operations."""

from __future__ import annotations

from typing import Any, Collection, Protocol, runtime_checkable

from pydantic import BaseModel

from aap_assets_media.contract import (
    OPERATIONS,
    PAGE_BYTES,
    REQUEST_BYTES,
    MediaFailure,
    MediaAssets,
    SamplesPage,
    TimetablePage,
    BackfillPage,
    decode_reply,
    json_bytes,
    require_scope,
)


@runtime_checkable
class MediaDatabase(Protocol):
    """Structural call port shared by the independent worker and API binding."""

    async def call(self, name: str, request: BaseModel) -> BaseModel: ...


def prepare_native_call(name: str, request: BaseModel) -> tuple[str, tuple[Any, ...]]:
    """Build a call only from a frozen operation name and its typed request."""
    operation = OPERATIONS.get(name)
    if operation is None or type(request) is not operation.request:
        raise TypeError("invalid media operation/request pair")
    if len(json_bytes(request.model_dump(mode="json"))) > REQUEST_BYTES:
        raise MediaFailure("resource_budget_exceeded")

    placeholders: list[str] = []
    parameters: list[Any] = []
    for index, (field, sql_type) in enumerate(
        zip(operation.arguments, operation.sql_types, strict=True), start=1
    ):
        placeholders.append(f"${index}::{sql_type}")
        value = getattr(request, field)
        if sql_type == "jsonb" and value is not None:
            value = json_bytes(
                value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            ).decode("utf-8")
        parameters.append(value)
    return (
        f"SELECT aap_assets_v1.{name}({', '.join(placeholders)})",
        tuple(parameters),
    )


def validate_native_result(name: str, request: BaseModel, raw: Any) -> BaseModel:
    """Decode, bound and verify the frozen response before its transaction commits."""
    operation = OPERATIONS.get(name)
    if operation is None or type(request) is not operation.request:
        raise TypeError("invalid media operation/request pair")
    result = decode_reply(raw, operation.result)
    try:
        require_scope(request, result)
        records: list[BaseModel] | None = None
        if isinstance(result, TimetablePage):
            records = result.frames
        elif isinstance(result, SamplesPage):
            records = result.samples
        elif isinstance(result, BackfillPage):
            records = result.items
        elif isinstance(result, MediaAssets):
            records = result.assets
        if records is not None and len(json_bytes(records)) > PAGE_BYTES:
            raise MediaFailure("resource_budget_exceeded")
    except MediaFailure:
        raise
    except (TypeError, ValueError, RecursionError):
        raise MediaFailure("invalid_response") from None
    return result


class AsyncpgMediaDatabase:
    """Execute one published native function per committed asyncpg transaction."""

    def __init__(
        self,
        pool: Any,
        *,
        allowed_roles: Collection[str] | None = None,
    ) -> None:
        self._pool = pool
        self._allowed_roles = (
            frozenset(allowed_roles) if allowed_roles is not None else None
        )

    @classmethod
    async def connect(
        cls,
        database_url: str,
        *,
        allowed_roles: Collection[str] | None = None,
    ) -> AsyncpgMediaDatabase:
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=1,
            max_size=1,
            timeout=5,
            command_timeout=10,
            server_settings={"application_name": "aap-assets-media-worker"},
        )
        return cls(pool, allowed_roles=allowed_roles)

    async def call(self, name: str, request: BaseModel) -> BaseModel:
        operation = OPERATIONS.get(name)
        if operation is None or type(request) is not operation.request:
            raise TypeError("invalid media operation/request pair")
        if (
            self._allowed_roles is not None
            and operation.role not in self._allowed_roles
        ):
            raise PermissionError("media operation is outside this SQL login scope")
        statement, parameters = prepare_native_call(name, request)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                raw = await connection.fetchval(statement, *parameters)
                return validate_native_result(name, request, raw)

    async def close(self) -> None:
        await self._pool.close()
