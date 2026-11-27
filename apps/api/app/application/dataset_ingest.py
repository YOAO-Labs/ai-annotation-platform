from __future__ import annotations

from dataclasses import dataclass
import mimetypes
import os
from typing import BinaryIO, Literal
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.services.dataset import DatasetService, ensure_dataset_storage_name_is_writable
from app.services.dataset_assets import find_verified_content
from app.services.dataset_source_access import validate_dataset_write
from app.services.dataset_storage_keys import (
    dataset_relative_path,
    managed_dataset_prefix,
)
from app.services.storage import storage_service
from app.services.system_settings_service import SystemSettingsService


@dataclass(frozen=True)
class IngestOutcome:
    status: Literal["added", "skipped", "error"]
    relpath: str
    item_id: uuid.UUID | None = None
    file_type: str | None = None
    file_size: int | None = None
    content_hash: str | None = None
    linked_tasks: int = 0
    reason: str | None = None
    error: str | None = None


class DatasetIngestWorkflow:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.assets = DatasetService(db)

    async def ingest_one(
        self,
        dataset_id: uuid.UUID,
        relpath: str,
        stream: BinaryIO | None = None,
        *,
        size: int | None = None,
        storage_key: str | None = None,
        object_etag: str | None = None,
        dest_relpath: str | None = None,
        directory_authority=None,
        max_bytes: int | None = None,
        creation_requested_by: uuid.UUID | None = None,
        creation_authority: dict | None = None,
    ) -> IngestOutcome:
        """Ingest one source object into a dataset.

        `storage_key` is for objects already present in the datasets bucket (scan);
        otherwise `stream` is copied into that bucket in chunks while hashing.

        `dest_relpath` 给定时（连接器/扫描导入）保留源子目录结构作为 file_name 与
        storage_key 后缀（如 `a/img.jpg`）；不给时退回纯文件名（basename），其它调用零影响。
        """
        await validate_dataset_write(self.db, dataset_id, directory_authority)
        ds = await self.assets.get(dataset_id)
        if not ds:
            return IngestOutcome(
                status="error", relpath=relpath, error="dataset missing"
            )
        try:
            ensure_dataset_storage_name_is_writable(ds.name)
        except HTTPException as exc:
            return IngestOutcome(
                status="error",
                relpath=relpath,
                error=str(exc.detail),
            )

        source_name = (
            _safe_relpath(dest_relpath) if dest_relpath else _safe_basename(relpath)
        )
        final_name = await self.assets._unique_file_name(dataset_id, source_name)
        base_for_ext = os.path.basename(final_name)
        ext = base_for_ext.rsplit(".", 1)[-1].lower() if "." in base_for_ext else ""
        file_type = _infer_file_type_from_ext(ext)
        creation = None

        if storage_key and storage_key.lstrip("/").startswith(
            "__aap_auxiliary_originals__/"
        ):
            raise HTTPException(422, "Auxiliary originals cannot become dataset items")

        if storage_key:
            duplicate_path = await self.assets._find_by_file_path(
                dataset_id, storage_key
            )
            if duplicate_path is not None:
                return IngestOutcome(
                    status="skipped",
                    relpath=relpath,
                    reason="file_path_exists",
                    item_id=duplicate_path.id,
                )
            content_hash = None
            sha256 = None
            file_size = size
            head_bytes = b""
        else:
            if stream is None:
                return IngestOutcome(
                    status="error", relpath=relpath, error="stream missing"
                )
            from app.services import managed_original_creation as originals

            if max_bytes is None:
                limits = await SystemSettingsService.get_import_limits_snapshot(self.db)
                max_bytes = limits["dataset_import_max_total_bytes"]
            content_type = (
                mimetypes.guess_type(final_name)[0] or "application/octet-stream"
            )
            allocation = await originals.allocate(
                self.db,
                dataset_id=dataset_id,
                logical_path=final_name,
                origin="connector",
                max_bytes=max_bytes,
                content_type=content_type,
                requested_by=creation_requested_by,
                authority=(
                    creation_authority
                    if creation_authority is not None
                    else directory_authority
                ),
            )
            storage_key = allocation["object_key"]
            await self.db.commit()
            creation = await originals.create_stream(
                self.db, allocation, stream, content_type=content_type
            )
            content_hash, sha256, file_size, head_bytes = (
                creation.md5,
                creation.sha256,
                creation.size_bytes,
                creation.head_bytes,
            )
            object_etag = creation.etag

        async def check_authority(*, lock=False):
            await validate_dataset_write(
                self.db, dataset_id, directory_authority, lock=lock
            )

        await check_authority()
        if sha256:
            existing = await find_verified_content(
                self.db, dataset_id, sha256, legacy_md5=content_hash
            )
            if existing is not None:
                return IngestOutcome(
                    status="skipped",
                    relpath=relpath,
                    item_id=existing.id,
                    file_type=existing.file_type,
                    file_size=file_size,
                    content_hash=content_hash,
                    reason="content_hash_exists",
                )

        width: int | None = None
        height: int | None = None
        if file_type == "image":
            dims = (
                storage_service.read_image_dimensions_from_bytes(head_bytes)
                if head_bytes
                else None
            )
            if dims is None and creation is not None:
                dims = await originals.image_dimensions(self.db, allocation, creation)
            elif dims is None:
                dims = storage_service.read_image_dimensions(
                    storage_key,
                    bucket=storage_service.datasets_bucket,
                )
            if dims:
                width, height = dims

        await check_authority(lock=True)
        if (
            creation is not None
            and final_name in await self.assets._existing_file_names(dataset_id)
        ):
            from app.services.content_snapshot import ContentError

            raise ContentError("managed_original_creation_input_changed")
        item = await self.assets.add_item(
            dataset_id=dataset_id,
            file_name=final_name,
            file_path=storage_key,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
            width=width,
            height=height,
            sha256=sha256,
            object_etag=object_etag,
            storage_binding=originals.original_locator(allocation)
            if creation is not None
            else None,
            storage_ref=originals.original_ref(allocation)
            if creation is not None
            else None,
            logical_path=final_name,
        )
        if creation is not None:
            await originals.attach(self.db, item, creation.creation_id)
        linked_tasks = await DatasetTaskCreationWorkflow(self.db).append_items(
            dataset_id, [item.id]
        )
        await check_authority()
        return IngestOutcome(
            status="added",
            relpath=relpath,
            item_id=item.id,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
            linked_tasks=linked_tasks,
        )

    async def scan_and_import(
        self, dataset_id: uuid.UUID, *, directory_authority=None
    ) -> list[IngestOutcome]:
        await validate_dataset_write(self.db, dataset_id, directory_authority)
        ds = await self.assets.get(dataset_id)
        if not ds:
            return []
        ensure_dataset_storage_name_is_writable(ds.name)

        outcomes: list[IngestOutcome] = []
        prefixes = dict.fromkeys(
            [ds.storage_scan_prefix or f"{ds.name}/", managed_dataset_prefix(ds.id)]
        )
        for prefix in prefixes:
            objects = storage_service.list_objects(
                prefix, bucket=storage_service.datasets_bucket
            )
            for obj in objects:
                key: str = obj["key"]
                if key.endswith("/") or not key.startswith(prefix):
                    continue
                rel_under_ds = (
                    dataset_relative_path(key)
                    if key.startswith(managed_dataset_prefix(ds.id))
                    else key[len(prefix) :]
                )
                outcomes.append(
                    await self.ingest_one(
                        dataset_id,
                        key,
                        size=obj.get("size"),
                        storage_key=key,
                        object_etag=obj.get("etag"),
                        dest_relpath=rel_under_ds,
                        directory_authority=directory_authority,
                    )
                )
        return outcomes


async def enqueue_media_for_items(db: AsyncSession, item_ids: list[uuid.UUID]) -> None:
    from app.services.reference_access import reject_reference_scope

    await reject_reference_scope(db, item_ids=item_ids)
    unique_item_ids = list(dict.fromkeys(item_ids))
    if not unique_item_ids:
        return
    from app.services.assets_spatial_views import labeling_dataset_item_facts

    item = labeling_dataset_item_facts
    rows = await db.execute(
        select(item.id, item.file_type).where(item.id.in_(unique_item_ids))
    )
    for item_id, file_type in rows.all():
        if file_type == "image":
            from app.workers.media import generate_thumbnail

            generate_thumbnail.delay(str(item_id))
        elif file_type == "video":
            from app.workers.media import generate_video_metadata

            from app.services import video_derivative_originals as originals

            source = await originals.pin(db, "dataset_item", item_id)
            delegation = originals.system_authorization("dataset_item", item_id, source)
            generate_video_metadata.delay(str(item_id), delegation=delegation)


_IMAGE_EXTS = {"jpg", "jpeg", "png", "bmp", "webp", "tiff", "tif", "gif", "svg"}
_VIDEO_EXTS = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm"}
_POINTCLOUD_EXTS = {"pcd", "bin", "las", "ply"}


def _safe_basename(relpath: str) -> str:
    normalized = (relpath or "").replace("\\", "/").rstrip("/")
    name = os.path.basename(normalized)
    return name or f"source-{uuid.uuid4().hex}"


def _safe_relpath(relpath: str) -> str:
    """归一化相对路径并保留子目录层级；遇到空 / 含 `..` 的不安全路径退回 basename。"""
    raw = (relpath or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in raw.split("/") if p not in {"", "."}]
    if not parts or any(p == ".." for p in parts):
        return _safe_basename(relpath)
    return "/".join(parts)


def _infer_file_type_from_ext(ext: str) -> str:
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _POINTCLOUD_EXTS:
        return "point_cloud"
    return "other"
