"""Bind the public catalog API to its request-time Assets SQL capability."""

from __future__ import annotations

import asyncio
import json
from typing import Callable, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_assets_catalog import (
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
from yoao_assets_catalog.associations_contract import (
    AssociationFailure,
    DatasetProjectsResult,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
    ProjectDatasetsResult,
    decode_associations_reply,
)
from yoao_assets_images.contract import ReadImageSourceRequest
from yoao_assets_images.transport import ImageSourceReference
from yoao_assets_media.contract import MediaFailure, ReadSourceRequest
from yoao_assets_media.transport import SourceReference

from app.services.assets_media import browser_media_url, media_gateway
from app.services.image_source_references import image_source_references
from app.services.storage import storage_service
from app.services.video_poster_references import video_source_references

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
_ASSOCIATION_ERRORS: dict[str, tuple[int, str, dict[str, str] | None]] = {
    "invalid_request": (422, "项目数据集关联读取请求无效", None),
    "account_not_active": (
        401,
        "账号已停用或不存在",
        {"WWW-Authenticate": "Bearer"},
    ),
    "invalid_account_role": (403, "账号角色无效，访问被拒绝", None),
    "project_missing": (404, "项目不存在", None),
    "invalid_project_role": (403, "项目职责无效，访问被拒绝", None),
    "incompatible_project_role": (403, "账号平台角色与项目职责不兼容", None),
}


class CatalogGatewayError(HTTPException):
    """A native catalog reply or capability failed."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 503,
        headers: dict[str, str] | None = None,
    ):
        self.code = code
        super().__init__(status_code=status_code, detail=message, headers=headers)


def _require_request_budget(request: BaseModel) -> None:
    body = request.model_dump_json(exclude_unset=True)
    if len(body.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise CatalogGatewayError(
            "invalid_request", "Assets catalog request is too large", 422
        )


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


def _decode_associations(payload: object, result_type: type[_RESULT]) -> _RESULT:
    try:
        return decode_associations_reply(payload, result_type)
    except AssociationFailure as exc:
        status_code, message, headers = _ASSOCIATION_ERRORS[exc.code]
        raise CatalogGatewayError(exc.code, message, status_code, headers) from None
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise CatalogGatewayError(
            "invalid_response", "Assets catalog returned an invalid response", 502
        ) from exc


class AssetsCatalogGateway:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list(self, request: ListDatasetsRequest) -> DatasetPage:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.list_catalog_datasets(
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
            DatasetPage,
        )
        if (reply.limit, reply.offset) != (request.limit, request.offset):
            raise self._invalid_scope()
        return reply

    async def get(self, request: GetDatasetRequest) -> DatasetResult:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.get_catalog_dataset(
                CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid)
            )
            """,
            {"actor_id": request.actor_id, "dataset_id": request.dataset_id},
            DatasetResult,
        )
        if reply.dataset.id != request.dataset_id:
            raise self._invalid_scope()
        return reply

    async def items(self, request: ListItemsRequest) -> ItemPage:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.list_catalog_items(
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
            ItemPage,
        )
        if (
            reply.dataset_id != request.dataset_id
            or (reply.limit, reply.offset) != (request.limit, request.offset)
            or any(item.dataset_id != request.dataset_id for item in reply.items)
        ):
            raise self._invalid_scope()
        image_items = [item for item in reply.items if item.file_type == "image"]
        image_references_by_id = {}
        if image_items:
            references = await image_source_references(
                self.db,
                [
                    ReadImageSourceRequest(
                        actor_id=request.actor_id,
                        dataset_item_id=item.id,
                    )
                    for item in image_items
                ],
            )
            image_references_by_id = {
                item.id: reference
                for item, reference in zip(image_items, references, strict=True)
            }
        video_items = [item for item in reply.items if item.file_type == "video"]
        video_references_by_id = {}
        if video_items:
            source_references = await video_source_references(
                self.db,
                [
                    ReadSourceRequest(
                        actor_id=request.actor_id,
                        dataset_item_id=item.id,
                    )
                    for item in video_items
                ],
            )
            video_references_by_id = {
                item.id: reference
                for item, reference in zip(video_items, source_references, strict=True)
            }
        other_items = [
            item for item in reply.items if item.file_type not in {"image", "video"}
        ]
        other_references_by_id = {}
        if other_items:
            try:
                async with media_gateway(self.db) as gateway:
                    for start in range(0, len(other_items), 4):
                        batch = other_items[start : start + 4]
                        references = await asyncio.gather(
                            *(
                                gateway.raw_source_reference(
                                    ReadSourceRequest(
                                        actor_id=request.actor_id,
                                        dataset_item_id=item.id,
                                    )
                                )
                                for item in batch
                            ),
                            return_exceptions=True,
                        )
                        failures = [
                            value
                            for value in references
                            if isinstance(value, BaseException)
                        ]
                        if any(
                            getattr(value, "storage_operation_unsettled", False)
                            for value in failures
                        ):
                            if len(failures) == 1:
                                raise failures[0]
                            combined = BaseExceptionGroup(
                                "Catalog source references failed", failures
                            )
                            combined.storage_operation_unsettled = True
                            raise combined
                        for item, reference in zip(batch, references, strict=True):
                            if isinstance(reference, BaseException):
                                if not isinstance(reference, Exception):
                                    raise reference
                                continue
                            other_references_by_id[item.id] = reference
            except MediaFailure as exc:
                if getattr(exc, "storage_operation_unsettled", False):
                    raise
                pass
        reply = reply.model_copy(
            update={
                "items": [
                    self._image_item(item, image_references_by_id.get(item.id))
                    if item.file_type == "image"
                    else self._video_item(item, video_references_by_id.get(item.id))
                    if item.file_type == "video"
                    else self._other_item(item, other_references_by_id.get(item.id))
                    for item in reply.items
                ]
            }
        )
        return reply

    async def create(self, request: CreateDatasetRequest) -> DatasetMutationResult:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.create_catalog_dataset(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:payload AS jsonb)
            )
            """,
            {
                "operation_id": request.operation_id,
                "actor_id": request.actor_id,
                "payload": json.dumps(request.native_payload(), separators=(",", ":")),
            },
            DatasetMutationResult,
        )
        if (
            reply.operation_id != request.operation_id
            or reply.actor_id != request.actor_id
            or reply.dataset.created_by != request.actor_id
        ):
            raise self._invalid_scope()
        await self.db.commit()
        return reply

    async def update(self, request: UpdateDatasetRequest) -> DatasetMutationResult:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.update_catalog_dataset(
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
            DatasetMutationResult,
        )
        if (
            reply.operation_id != request.operation_id
            or reply.actor_id != request.actor_id
            or reply.dataset.id != request.dataset_id
        ):
            raise self._invalid_scope()
        await self.db.commit()
        return reply

    async def list_dataset_projects(
        self, request: ListDatasetProjectsRequest
    ) -> DatasetProjectsResult:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.list_dataset_projects(
                CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid)
            )
            """,
            {"actor_id": request.actor_id, "dataset_id": request.dataset_id},
            DatasetProjectsResult,
            decoder=_decode_associations,
            local_errors_unavailable=True,
        )
        if reply.dataset_id != request.dataset_id:
            raise self._invalid_scope()
        return reply

    async def list_project_datasets(
        self, request: ListProjectDatasetsRequest
    ) -> ProjectDatasetsResult:
        _require_request_budget(request)
        reply = await self._call(
            """
            SELECT yoao_assets_v1.list_project_datasets(
                CAST(:actor_id AS uuid), CAST(:project_id AS uuid)
            )
            """,
            {"actor_id": request.actor_id, "project_id": request.project_id},
            ProjectDatasetsResult,
            decoder=_decode_associations,
            local_errors_unavailable=True,
        )
        if reply.project_id != request.project_id:
            raise self._invalid_scope()
        return reply

    async def _call(
        self,
        sql: str,
        parameters: dict,
        result_type: type[_RESULT],
        *,
        decoder: Callable[[object, type[_RESULT]], _RESULT] = _decode,
        local_errors_unavailable: bool = False,
    ) -> _RESULT:
        try:
            result = await self.db.execute(text(sql), parameters)
            payload = result.scalar_one()
        except SQLAlchemyError:
            if local_errors_unavailable:
                raise CatalogGatewayError(
                    "unavailable", "Assets catalog service is unavailable", 503
                ) from None
            raise
        return decoder(payload, result_type)

    @staticmethod
    def _invalid_scope() -> CatalogGatewayError:
        return CatalogGatewayError(
            "invalid_response", "Assets catalog returned an invalid response", 502
        )

    @staticmethod
    def _other_item(
        item: CatalogItem, reference: SourceReference | None
    ) -> CatalogItem:
        source = reference.source if reference is not None else None
        url = thumbnail_url = None
        if (
            source is not None
            and source.dataset_item_id == item.id
            and source.dataset_id == item.dataset_id
            and source.file_path == item.file_path
            and source.file_type == item.file_type
            and source.file_size == item.file_size
            and source.content_hash == item.content_hash
            and not source.source_drifted
            and reference.original_url is not None
        ):
            url = browser_media_url(reference.original_url)
            if item.thumbnail_path:
                try:
                    bucket = storage_service.bucket_for_cache_key(
                        item.thumbnail_path, default=storage_service.datasets_bucket
                    )
                    thumbnail_url = storage_service.generate_download_url(
                        item.thumbnail_path, bucket=bucket
                    )
                except Exception:
                    pass
        return item.model_copy(update={"file_url": url, "thumbnail_url": thumbnail_url})

    @staticmethod
    def _image_item(
        item: CatalogItem, reference: ImageSourceReference | None
    ) -> CatalogItem:
        updates: dict[str, object] = {
            "file_url": None,
            "thumbnail_url": None,
            "thumbnail_path": None,
            "blurhash": None,
        }
        if reference is not None:
            source = reference.source
            matches_item = (
                source.owner.kind == "dataset_item"
                and source.owner.id == item.id
                and source.item_id == item.id
                and source.dataset_id == item.dataset_id
                and source.file_path == item.file_path
                and source.file_size == item.file_size
                and source.content_hash == item.content_hash
            )
            if matches_item:
                updates.update(
                    {
                        "file_url": (
                            browser_media_url(reference.original_url)
                            if not source.source_drifted
                            and reference.original_url is not None
                            else None
                        ),
                        "thumbnail_url": browser_media_url(reference.thumbnail_url),
                        "thumbnail_path": (
                            source.thumbnail_path if source.thumbnail_current else None
                        ),
                        "blurhash": (
                            source.blurhash if source.thumbnail_current else None
                        ),
                        "width": source.width,
                        "height": source.height,
                    }
                )
        return item.model_copy(update=updates)

    @staticmethod
    def _video_item(
        item: CatalogItem, reference: SourceReference | None
    ) -> CatalogItem:
        return item.model_copy(
            update={
                "file_url": (
                    browser_media_url(reference.original_url)
                    if reference is not None
                    and not reference.source.source_drifted
                    and reference.original_url is not None
                    else None
                ),
                "thumbnail_url": (
                    reference.poster_url if reference is not None else None
                ),
                "blurhash": None,
            }
        )
