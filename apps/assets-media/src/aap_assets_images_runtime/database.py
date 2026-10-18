"""Transactional binding for the frozen image native capabilities."""

from __future__ import annotations

import re
from typing import Any, Collection, Protocol, runtime_checkable

from pydantic import BaseModel

from aap_assets_images.contract import (
    OPERATIONS,
    PAGE_BYTES,
    REQUEST_BYTES,
    REPLY_BYTES,
    ClaimedImageWork,
    ClaimImageMediaRequest,
    FinishImageMediaRequest,
    ImageFailure,
    decode_reply,
    json_bytes,
    require_scope,
)

_VALID_ROLES = frozenset({"control", "system", "worker"})
_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@runtime_checkable
class ImageDatabase(Protocol):
    async def call(self, name: str, request: BaseModel) -> BaseModel: ...


def prepare_native_call(
    name: str,
    request: BaseModel,
    *,
    allowed_roles: Collection[str] = ("worker",),
) -> tuple[str, tuple[Any, ...]]:
    operation = OPERATIONS.get(name)
    if operation is None or type(request) is not operation.request:
        raise TypeError("invalid image operation/request pair")
    if operation.role not in allowed_roles:
        raise PermissionError("image operation is outside this SQL login scope")
    if len(json_bytes(request.model_dump(mode="json", by_alias=True))) > REQUEST_BYTES:
        raise ImageFailure("resource_budget_exceeded")

    placeholders: list[str] = []
    parameters: list[Any] = []
    for index, (field, sql_type) in enumerate(
        zip(operation.arguments, operation.sql_types, strict=True), start=1
    ):
        placeholders.append(f"${index}::{sql_type}")
        value = getattr(request, field)
        if sql_type == "jsonb" and value is not None:
            value = json_bytes(
                value.model_dump(mode="json", by_alias=True)
                if isinstance(value, BaseModel)
                else value
            ).decode("utf-8")
        parameters.append(value)
    return (
        f"SELECT aap_assets_v1.{name}({', '.join(placeholders)})",
        tuple(parameters),
    )


def _validate_finish_request(request: FinishImageMediaRequest) -> None:
    """Reject outputs that cannot belong to this exact native lease before SQL."""
    result = request.result
    if result.kind == "dimensions":
        return
    token = request.lease_token
    generation = request.generation
    if result.kind == "thumbnail":
        expected = (
            f"image-assets/{request.job_id}/g{generation}/a{token}/thumbnail.webp"
        )
        if result.artifact.key != expected:
            raise ImageFailure("invalid_request")
        return
    match = re.fullmatch(
        rf"image-pyramids/({_UUID_PATTERN})/g{generation}/a{token}/",
        result.manifest_artifact.key.removesuffix("manifest.json"),
    )
    if match is None:
        raise ImageFailure("invalid_request")
    prefix = match.group(0)
    if (
        result.manifest.generation != generation
        or result.manifest_artifact.key != prefix + "manifest.json"
        or result.overview_artifact.key != prefix + "overview.webp"
        or any(
            tile.artifact.key != prefix + f"tiles/{tile.level}/{tile.x}/{tile.y}.webp"
            for tile in result.tile_artifacts
        )
    ):
        raise ImageFailure("invalid_request")


def validate_native_result(
    name: str,
    request: BaseModel,
    raw: Any,
) -> BaseModel:
    operation = OPERATIONS.get(name)
    if operation is None or type(request) is not operation.request:
        raise TypeError("invalid image operation/request pair")
    result = decode_reply(raw, operation.result)
    try:
        require_scope(request, result)
        if isinstance(request, ClaimImageMediaRequest) and isinstance(
            result, ClaimedImageWork
        ):
            claim = result.claim
            if claim is not None and (
                (request.job_id is not None and claim.job_id != request.job_id)
                or claim.spec.kind not in request.kinds
            ):
                raise ValueError("image claim escaped its requested selector")
        if isinstance(request, FinishImageMediaRequest):
            _validate_finish_request(request)
        records = getattr(result, "items", None)
        if records is None:
            records = getattr(result, "candidates", None)
        if isinstance(records, list) and len(json_bytes(records)) > PAGE_BYTES:
            raise ImageFailure("resource_budget_exceeded")
    except ImageFailure:
        raise
    except (TypeError, ValueError, RecursionError):
        raise ImageFailure("invalid_response") from None
    if len(json_bytes(result.model_dump(mode="json", by_alias=True))) > REPLY_BYTES:
        raise ImageFailure("resource_budget_exceeded")
    return result


class AsyncpgImageDatabase:
    """Run one frozen image function per validated PostgreSQL transaction."""

    def __init__(
        self,
        pool: Any,
        *,
        allowed_roles: Collection[str] = ("worker",),
    ) -> None:
        roles = frozenset(allowed_roles)
        if not roles or not roles <= _VALID_ROLES:
            raise ValueError("invalid image SQL operation roles")
        self._pool = pool
        self._allowed_roles = roles

    @property
    def pool(self) -> Any:
        return self._pool

    @classmethod
    async def connect(
        cls,
        database_url: str,
        *,
        allowed_roles: Collection[str] = ("worker",),
    ) -> AsyncpgImageDatabase:
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=1,
            max_size=1,
            timeout=5,
            command_timeout=30,
            server_settings={"application_name": "aap-assets-images-worker"},
        )
        return cls(pool, allowed_roles=allowed_roles)

    async def call(self, name: str, request: BaseModel) -> BaseModel:
        operation = OPERATIONS.get(name)
        if operation is None or type(request) is not operation.request:
            raise TypeError("invalid image operation/request pair")
        if operation.role not in self._allowed_roles:
            raise PermissionError("image operation is outside this SQL login scope")
        statement, parameters = prepare_native_call(
            name, request, allowed_roles=self._allowed_roles
        )
        if isinstance(request, FinishImageMediaRequest):
            _validate_finish_request(request)
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                raw = await connection.fetchval(statement, *parameters)
                return validate_native_result(
                    name,
                    request,
                    raw,
                )

    async def close(self) -> None:
        await self._pool.close()
