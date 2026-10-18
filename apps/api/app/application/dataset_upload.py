"""Publish staged uploads through the Assets and Labeling data capabilities."""

import asyncio
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID, uuid4

from fastapi import HTTPException
from botocore.exceptions import ClientError
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.services.dataset import DatasetService
from app.services.dataset_upload_contract import (
    DatasetUploadCompletedResult,
    DatasetUploadCompletion,
    DatasetUploadDiscard,
    DatasetUploadFailure,
    DatasetUploadIntent,
    DatasetUploadReserve,
)
from app.services.dataset_upload_data import DatasetUploadData
from app.services.storage import StorageService


def restore_upload_failure(exc: DatasetUploadFailure) -> NoReturn:
    if exc.code == "account_not_active":
        raise HTTPException(
            401, "账号已停用或不存在", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    if exc.code in {"upload_missing", "dataset_missing"}:
        raise HTTPException(
            404,
            "Item not found" if exc.code == "upload_missing" else "Dataset not found",
        ) from exc
    if exc.code in {"upload_scope_mismatch", "invalid_account_role"}:
        raise HTTPException(403, "上传操作不属于当前账号或素材集") from exc
    if exc.code == "invalid_request":
        raise HTTPException(422, "上传参数无效") from exc
    raise HTTPException(409, {"reason": exc.code}) from exc


@dataclass(frozen=True)
class DatasetUploadOutcome:
    item_id: UUID
    file_type: str
    result: DatasetUploadCompletedResult | None = None
    duplicate_of: UUID | None = None


class DatasetUploadWorkflow:
    def __init__(self, db: AsyncSession, storage: StorageService):
        self.db = db
        self.storage = storage
        self.data = DatasetUploadData(db)

    async def reserve(
        self, *, actor_id: UUID, dataset_id: UUID, file_name: str, content_type: str
    ) -> DatasetUploadIntent:
        try:
            return await self.data.reserve(
                DatasetUploadReserve(
                    item_id=uuid4(),
                    actor_id=actor_id,
                    dataset_id=dataset_id,
                    file_name=file_name,
                    content_type=content_type,
                )
            )
        except DatasetUploadFailure as exc:
            restore_upload_failure(exc)

    async def complete(
        self, *, actor_id: UUID, dataset_id: UUID, item_id: UUID
    ) -> DatasetUploadOutcome:
        try:
            intent = await self.data.lock(
                actor_id=actor_id, dataset_id=dataset_id, item_id=item_id
            )
        except DatasetUploadFailure as exc:
            if exc.code == "upload_missing":
                return await self._complete_legacy(
                    dataset_id=dataset_id, item_id=item_id
                )
            restore_upload_failure(exc)
        if intent.state == "completed":
            assert isinstance(intent.result, DatasetUploadCompletedResult)
            return DatasetUploadOutcome(item_id, intent.file_type, result=intent.result)
        if intent.state == "discarded":
            assert intent.result is not None
            if intent.result.duplicate_of is not None:
                return DatasetUploadOutcome(
                    item_id, intent.file_type, duplicate_of=intent.result.duplicate_of
                )
            if intent.result.reason == "missing":
                return DatasetUploadOutcome(item_id, intent.file_type)
            raise HTTPException(409, {"reason": "upload_not_pending"})
        meta = await asyncio.to_thread(
            self.storage.verify_upload,
            intent.staging_key,
            bucket=self.storage.import_bucket,
        )
        if meta is None:
            try:
                await self.data.discard(
                    DatasetUploadDiscard(
                        actor_id=actor_id,
                        dataset_id=dataset_id,
                        item_id=item_id,
                        reason="missing",
                    )
                )
            except DatasetUploadFailure as exc:
                restore_upload_failure(exc)
            return DatasetUploadOutcome(item_id, intent.file_type)
        size, etag, content_hash = _upload_metadata(meta)
        try:
            duplicate = await self.data.find_duplicate(
                actor_id=actor_id,
                dataset_id=dataset_id,
                item_id=item_id,
                content_hash=content_hash,
            )
        except DatasetUploadFailure as exc:
            restore_upload_failure(exc)
        dimensions = None
        if duplicate.duplicate_of is None:
            if intent.file_type == "image":
                dimensions = await asyncio.to_thread(
                    self.storage.read_image_dimensions,
                    intent.staging_key,
                    bucket=self.storage.import_bucket,
                    if_match=etag,
                )
            assert intent.file_path is not None
            # shortcut: Keep uncertain source copies; fenced publication receipts are required before reclaiming them.
            try:
                await asyncio.to_thread(
                    self.storage.publish_dataset_upload,
                    intent.staging_key,
                    intent.file_path,
                    etag=etag,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {
                    "412",
                    "PreconditionFailed",
                }:
                    restore_upload_failure(DatasetUploadFailure("source_changed"))
                raise
        assert intent.publication_id is not None
        try:
            accepted = await self.data.complete(
                DatasetUploadCompletion(
                    actor_id=actor_id,
                    dataset_id=dataset_id,
                    item_id=item_id,
                    publication_id=intent.publication_id,
                    file_size=size,
                    content_hash=content_hash,
                    width=dimensions[0] if dimensions else None,
                    height=dimensions[1] if dimensions else None,
                )
            )
        except DatasetUploadFailure as exc:
            restore_upload_failure(exc)
        if accepted.disposition == "duplicate":
            return DatasetUploadOutcome(
                item_id, intent.file_type, duplicate_of=accepted.duplicate_of
            )
        self._invalidate_counters(dataset_id)
        assert isinstance(accepted.result, DatasetUploadCompletedResult)
        return DatasetUploadOutcome(item_id, intent.file_type, result=accepted.result)

    def _invalidate_counters(self, dataset_id: UUID) -> None:
        for row in self.db.sync_session.identity_map.values():
            if isinstance(row, Project):
                self.db.expire(row, ["total_tasks", "updated_at"])
            elif isinstance(row, Dataset) and inspect(row).identity == (dataset_id,):
                self.db.expire(row, ["file_count", "updated_at"])

    async def _complete_legacy(
        self, *, dataset_id: UUID, item_id: UUID
    ) -> DatasetUploadOutcome:
        # Old placeholders have no provable upload actor; compatibility never deletes them or their source.
        service = DatasetService(self.db)
        if await service.lock_dataset_reference(dataset_id) is None:
            raise HTTPException(404, "Item not found")
        item = await self.db.get(DatasetItem, item_id, populate_existing=True)
        if item is None or item.dataset_id != dataset_id:
            raise HTTPException(404, "Item not found")
        meta = await asyncio.to_thread(
            self.storage.verify_upload,
            item.file_path,
            bucket=self.storage.datasets_bucket,
        )
        if meta is None:
            return DatasetUploadOutcome(item_id, item.file_type)
        size, etag, content_hash = _upload_metadata(meta)
        if content_hash is not None:
            existing = await service.find_by_hash(dataset_id, content_hash)
            if existing is not None and existing.id != item_id:
                return DatasetUploadOutcome(
                    item_id, item.file_type, duplicate_of=existing.id
                )
        service.record_uploaded_item_content(
            item, file_size=size, content_hash=content_hash
        )
        if item.file_type == "image" and (item.width is None or item.height is None):
            dimensions = await asyncio.to_thread(
                self.storage.read_image_dimensions,
                item.file_path,
                bucket=self.storage.datasets_bucket,
                if_match=etag,
            )
            if dimensions:
                await service.set_item_dimensions(item_id, dimensions)
        linked = await DatasetTaskCreationWorkflow(self.db).append_items(
            dataset_id, [item_id]
        )
        return DatasetUploadOutcome(
            item_id,
            item.file_type,
            result=DatasetUploadCompletedResult(item_id=item_id, linked_tasks=linked),
        )


def _upload_metadata(meta: dict) -> tuple[int, str, str | None]:
    size, etag = meta.get("ContentLength"), meta.get("ETag")
    if (
        type(size) is not int
        or size < 0
        or not isinstance(etag, str)
        or not etag
        or len(etag) > 1024
        or any(char in etag for char in "\r\n")
    ):
        raise HTTPException(502, "Invalid object storage metadata")
    candidate = etag.strip('"')
    content_hash = (
        candidate.lower()
        if len(candidate) == 32
        and all(char in "0123456789abcdefABCDEF" for char in candidate)
        else None
    )
    return size, etag, content_hash
