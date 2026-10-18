"""Bind the public catalog API to its local SQL capability or remote owner."""

from __future__ import annotations

import json
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog import (
    CatalogFailure,
    CatalogItem,
    CreateDatasetRequest,
    DatasetMutationResult,
    DatasetPage,
    DatasetResult,
    GetDatasetRequest,
    ItemPage,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
    decode_reply,
)
from app.config import settings
from app.services.storage import storage_service

_MAX_REQUEST_BYTES = 1 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_RESULT = TypeVar("_RESULT", bound=BaseModel)

_BUSINESS_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_request": (422, "Invalid dataset catalog request"),
    "account_not_active": (401, "Account is not active"),
    "account_busy": (409, "Account is busy"),
    "invalid_account_role": (403, "Account role is invalid"),
    "catalog_manager_required": (403, "Catalog manager role required"),
    "dataset_missing": (404, "Dataset not found"),
    "source_busy": (409, "Dataset has an active operation"),
    "operation_conflict": (409, "Idempotency-Key conflicts with an earlier request"),
    "reserved_namespace": (422, "Dataset name uses a reserved storage namespace"),
}


def _peer_url() -> str | None:
    value = settings.assets_catalog_url
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    token = settings.assets_catalog_token
    if (
        not valid
        or len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise CatalogGatewayError("unavailable", "Invalid Assets catalog configuration")
    return value.rstrip("/")


class CatalogGatewayError(HTTPException):
    """A selected peer or its response failed; local SQL is never a fallback."""

    def __init__(self, code: str, message: str, status_code: int = 503):
        self.code = code
        super().__init__(status_code=status_code, detail=message)


def _encode_request(request: BaseModel) -> bytes:
    body = request.model_dump_json(exclude_unset=True).encode("utf-8")
    if len(body) > _MAX_REQUEST_BYTES:
        raise CatalogGatewayError(
            "invalid_request", "Assets catalog request is too large", 422
        )
    return body


async def _read_response(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise CatalogGatewayError(
                "invalid_response", "Assets catalog response is too large", 502
            )
    return bytes(body)


def _decode(payload: object, result_type: type[_RESULT]) -> _RESULT:
    try:
        return decode_reply(payload, result_type)
    except CatalogFailure as exc:
        status_code, message = _BUSINESS_ERRORS[exc.code]
        raise CatalogGatewayError(exc.code, message, status_code) from None
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise CatalogGatewayError(
            "invalid_response", "Assets catalog returned an invalid response", 502
        ) from exc


class AssetsCatalogGateway:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list(self, request: ListDatasetsRequest) -> DatasetPage:
        body = _encode_request(request)
        reply, _ = await self._call(
            "list",
            """
            SELECT aap_assets_v1.list_catalog_datasets(
                CAST(:actor_id AS uuid), CAST(:search AS text),
                CAST(:data_type AS text), CAST(:has_scenes AS boolean),
                CAST(:limit AS integer), CAST(:offset AS bigint)
            )
            """,
            {
                "actor_id": request.actor_id,
                "search": request.search,
                "data_type": request.data_type,
                "has_scenes": request.has_scenes,
                "limit": request.limit,
                "offset": request.offset,
            },
            body,
            DatasetPage,
        )
        if (reply.limit, reply.offset) != (request.limit, request.offset):
            raise self._invalid_scope()
        return reply

    async def get(self, request: GetDatasetRequest) -> DatasetResult:
        body = _encode_request(request)
        reply, _ = await self._call(
            "get",
            """
            SELECT aap_assets_v1.get_catalog_dataset(
                CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid)
            )
            """,
            {"actor_id": request.actor_id, "dataset_id": request.dataset_id},
            body,
            DatasetResult,
        )
        if reply.dataset.id != request.dataset_id:
            raise self._invalid_scope()
        return reply

    async def items(self, request: ListItemsRequest) -> ItemPage:
        body = _encode_request(request)
        reply, remote = await self._call(
            "items",
            """
            SELECT aap_assets_v1.list_catalog_items(
                CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid),
                CAST(:limit AS integer), CAST(:offset AS bigint)
            )
            """,
            {
                "actor_id": request.actor_id,
                "dataset_id": request.dataset_id,
                "limit": request.limit,
                "offset": request.offset,
            },
            body,
            ItemPage,
        )
        if (
            reply.dataset_id != request.dataset_id
            or (reply.limit, reply.offset) != (request.limit, request.offset)
            or any(item.dataset_id != request.dataset_id for item in reply.items)
        ):
            raise self._invalid_scope()
        if not remote:
            reply = reply.model_copy(
                update={"items": [self._sign_item(item) for item in reply.items]}
            )
        return reply

    async def create(self, request: CreateDatasetRequest) -> DatasetMutationResult:
        body = _encode_request(request)
        reply, remote = await self._call(
            "create",
            """
            SELECT aap_assets_v1.create_catalog_dataset(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:payload AS jsonb)
            )
            """,
            {
                "operation_id": request.operation_id,
                "actor_id": request.actor_id,
                "payload": json.dumps(request.native_payload(), separators=(",", ":")),
            },
            body,
            DatasetMutationResult,
        )
        if (
            reply.operation_id != request.operation_id
            or reply.actor_id != request.actor_id
            or reply.dataset.created_by != request.actor_id
        ):
            raise self._invalid_scope()
        if not remote:
            await self.db.commit()
        return reply

    async def update(self, request: UpdateDatasetRequest) -> DatasetMutationResult:
        body = _encode_request(request)
        reply, remote = await self._call(
            "update",
            """
            SELECT aap_assets_v1.update_catalog_dataset(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:payload AS jsonb)
            )
            """,
            {
                "operation_id": request.operation_id,
                "actor_id": request.actor_id,
                "dataset_id": request.dataset_id,
                "payload": json.dumps(request.native_payload(), separators=(",", ":")),
            },
            body,
            DatasetMutationResult,
        )
        if (
            reply.operation_id != request.operation_id
            or reply.actor_id != request.actor_id
            or reply.dataset.id != request.dataset_id
        ):
            raise self._invalid_scope()
        if not remote:
            await self.db.commit()
        return reply

    async def _call(
        self,
        operation: str,
        sql: str,
        parameters: dict,
        body: bytes,
        result_type: type[_RESULT],
    ) -> tuple[_RESULT, bool]:
        peer = _peer_url()
        if peer is None:
            # Native capability call shares the caller's transaction and actor identity.
            result = await self.db.execute(text(sql), parameters)
            return _decode(result.scalar_one(), result_type), False
        try:
            async with httpx.AsyncClient(
                timeout=settings.assets_catalog_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{peer}/v1/catalog/{operation}",
                    headers={
                        "Authorization": f"Bearer {settings.assets_catalog_token}",
                        "Content-Type": "application/json",
                    },
                    content=body,
                ) as response:
                    if response.status_code != 200:
                        status_code = (
                            503
                            if response.status_code >= 500
                            or response.status_code in {401, 403}
                            else 502
                        )
                        raise CatalogGatewayError(
                            "unavailable",
                            "Assets catalog service is unavailable",
                            status_code,
                        )
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise CatalogGatewayError(
                            "invalid_response",
                            "Assets catalog returned an invalid response",
                            502,
                        )
                    payload = json.loads(await _read_response(response))
        except CatalogGatewayError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CatalogGatewayError(
                "unavailable", "Assets catalog service is unavailable", 503
            ) from exc
        return _decode(payload, result_type), True

    @staticmethod
    def _invalid_scope() -> CatalogGatewayError:
        return CatalogGatewayError(
            "invalid_response", "Assets catalog returned an invalid response", 502
        )

    @staticmethod
    def _sign_item(item: CatalogItem) -> CatalogItem:
        updates: dict[str, str | None] = {"file_url": None, "thumbnail_url": None}
        try:
            updates["file_url"] = storage_service.generate_download_url(
                item.file_path, bucket=storage_service.datasets_bucket
            )
        except Exception:
            pass
        if item.thumbnail_path:
            try:
                bucket = storage_service.bucket_for_cache_key(
                    item.thumbnail_path, default=storage_service.datasets_bucket
                )
                updates["thumbnail_url"] = storage_service.generate_download_url(
                    item.thumbnail_path, bucket=bucket
                )
            except Exception:
                pass
        return item.model_copy(update=updates)
