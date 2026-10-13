from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import mimetypes
import os
import uuid
from typing import BinaryIO, Literal

from fastapi import HTTPException
from sqlalchemy import select, func, delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJobKind
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services import async_job as async_job_svc
from app.services.display_id import next_display_id
from app.services.project_kind import (
    dataset_has_scenes,
    dataset_kind,
    kind_mismatch_detail,
    media_kind_aliases,
    project_kind,
)
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from app.services.system_settings_service import SystemSettingsService

_STREAM_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_IMAGE_HEAD_BYTES = 256 * 1024

# v0.13.11 · 用于 DatasetService.update 区分「未传参」与「显式传 None」(后者表示清除
# metadata_["axis_convention"]，回退到默认 iso_8855)。
_UNSET: object = object()
log = logging.getLogger(__name__)


def ensure_dataset_storage_name_is_writable(name: str) -> None:
    first_part = name.strip("/").split("/", 1)[0]
    if first_part.startswith(TRUSTED_NUSCENES_PREFIX):
        raise HTTPException(
            status_code=422,
            detail="Dataset name uses a reserved storage namespace",
        )


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


@dataclass(frozen=True)
class LinkProjectResult:
    """v0.12.0 · link_project 返回结构：让 endpoint 拿到是否需要异步建 task。

    async_job_id 非 None 时表示大 dataset 走 Celery 异步，endpoint 须在
    db.commit() 之后再 enqueue（确保 link 行对 worker 可见）；created_tasks
    仅在同步路径有意义。
    """

    link: ProjectDataset
    async_job_id: uuid.UUID | None = None
    created_tasks: int = 0


@dataclass(frozen=True)
class DatasetDeleteCleanup:
    pyramid_asset_ids: tuple[uuid.UUID, ...]


async def build_tasks_for_link(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    chunk_size: int = 5000,
) -> dict:
    """v0.12.0 · 为 dataset link 分块建 task（worker / 同步快路径共用）。

    幂等：用 NOT EXISTS 过滤掉该 project 下已有 task 的 dataset_item，重复跑不双建。
    每块独立 commit（增量累加 project.total_tasks），传 job_id 时每块按 5% 粒度
    update_progress，供前端轮询看到进度。返回 {"created": N, "total": M}。

    v0.13.1 · 点云项目（project.data_type == "lidar"）分流到 scene 感知建任务器：
    先写各相机标定，再按帧建 Task + 多文件 link（见 services/pointcloud_import.py）。
    """
    project = await db.get(Project, project_id)
    if project and project.data_type == "lidar":
        from app.services.pointcloud_import import (
            attach_calibration,
            build_pointcloud_tasks_for_link,
        )

        await attach_calibration(db, dataset_id=dataset_id)
        # 标定独立落库：build_pointcloud 在「帧全已建过 task」时 total==0 早退且不 commit，
        # 若不在此 commit，attach_calibration 的 flush 会在 session 关闭时回滚 —— 重导入
        # （如修正 calib 文件后重跑）将刷不掉旧标定。commit 在这让标定持久化与建任务解耦。
        await db.commit()
        return await build_pointcloud_tasks_for_link(
            db,
            dataset_id=dataset_id,
            project_id=project_id,
            job_id=job_id,
            chunk_size=chunk_size,
        )

    item_q = (
        select(
            DatasetItem.id,
            DatasetItem.file_name,
            DatasetItem.file_path,
            DatasetItem.file_type,
        )
        .where(DatasetItem.dataset_id == dataset_id)
        .where(
            ~select(Task.id)
            .where(
                Task.project_id == project_id,
                Task.dataset_item_id == DatasetItem.id,
            )
            .exists()
        )
        .order_by(DatasetItem.id)
    )
    items = (await db.execute(item_q)).all()
    total = len(items)
    if total == 0:
        return {"created": 0, "total": 0}

    created = 0
    last_pct = 0
    for start in range(0, total, chunk_size):
        chunk = items[start : start + chunk_size]
        seq_result = await db.execute(
            text("SELECT nextval('display_seq_tasks') FROM generate_series(1, :n)"),
            {"n": len(chunk)},
        )
        display_nums = [row[0] for row in seq_result.all()]
        rows = [
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "dataset_item_id": item.id,
                "batch_id": None,
                "display_id": f"T-{display_nums[i]}",
                "file_name": item.file_name,
                "file_path": item.file_path,
                "file_type": item.file_type,
                "status": "pending",
                # A2 · known-empty annotation-phase accumulator for a task created
                # after every producer records.
                "annotation_contributor_ids": [],
            }
            for i, item in enumerate(chunk)
        ]
        await db.execute(insert(Task), rows)
        created += len(chunk)

        project = await db.get(Project, project_id)
        if project:
            project.total_tasks = (project.total_tasks or 0) + len(chunk)

        if job_id is not None:
            pct = int(created / total * 100)
            if pct >= last_pct + 5 or created == total:
                await async_job_svc.update_progress(db, job_id, pct)
                last_pct = pct

        await db.commit()

    return {"created": created, "total": total}


class DatasetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list(
        self,
        search: str | None = None,
        data_type: str | None = None,
        has_scenes: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        q = select(Dataset)
        count_q = select(func.count()).select_from(Dataset)

        if search:
            q = q.where(Dataset.name.ilike(f"%{search}%"))
            count_q = count_q.where(Dataset.name.ilike(f"%{search}%"))
        if data_type:
            # 按 media kind 过滤,与 link 硬门同口径(lidar/point_cloud 互通)。
            data_type_cond = Dataset.data_type.in_(media_kind_aliases(data_type))
            q = q.where(data_type_cond)
            count_q = count_q.where(data_type_cond)
        if has_scenes is not None:
            scenes_exists = (
                select(Scene.id).where(Scene.dataset_id == Dataset.id).limit(1).exists()
            )
            q = q.where(scenes_exists if has_scenes else ~scenes_exists)
            count_q = count_q.where(scenes_exists if has_scenes else ~scenes_exists)

        total = (await self.db.execute(count_q)).scalar() or 0
        result = await self.db.execute(
            q.order_by(Dataset.created_at.desc()).limit(limit).offset(offset)
        )
        datasets = result.scalars().all()

        ds_ids = [ds.id for ds in datasets]

        # 批量聚合 project_count 与 total_size，避免 N+1
        if ds_ids:
            pc_rows = await self.db.execute(
                select(ProjectDataset.dataset_id, func.count())
                .where(ProjectDataset.dataset_id.in_(ds_ids))
                .group_by(ProjectDataset.dataset_id)
            )
            pc_map = {r[0]: r[1] for r in pc_rows.all()}

            sz_rows = await self.db.execute(
                select(
                    DatasetItem.dataset_id,
                    func.coalesce(func.sum(DatasetItem.file_size), 0),
                )
                .where(DatasetItem.dataset_id.in_(ds_ids))
                .group_by(DatasetItem.dataset_id)
            )
            sz_map = {r[0]: int(r[1]) for r in sz_rows.all()}

            scene_rows = await self.db.execute(
                select(Scene.dataset_id)
                .where(Scene.dataset_id.in_(ds_ids))
                .group_by(Scene.dataset_id)
            )
            has_scenes_map = {r[0]: True for r in scene_rows.all()}
        else:
            pc_map: dict = {}
            sz_map: dict = {}
            has_scenes_map: dict = {}

        items = []
        for ds in datasets:
            items.append(
                {
                    **_dataset_dict(ds),
                    "project_count": pc_map.get(ds.id, 0),
                    "total_size": sz_map.get(ds.id, 0),
                    "has_scenes": has_scenes_map.get(ds.id, False),
                }
            )

        return items, total

    async def get(self, dataset_id: uuid.UUID) -> Dataset | None:
        return await self.db.get(Dataset, dataset_id)

    async def get_with_project_count(self, dataset_id: uuid.UUID) -> dict | None:
        ds = await self.db.get(Dataset, dataset_id)
        if not ds:
            return None
        pc = (
            await self.db.execute(
                select(func.count())
                .select_from(ProjectDataset)
                .where(ProjectDataset.dataset_id == ds.id)
            )
        ).scalar() or 0
        total_size = (
            await self.db.execute(
                select(func.coalesce(func.sum(DatasetItem.file_size), 0)).where(
                    DatasetItem.dataset_id == ds.id
                )
            )
        ).scalar() or 0
        has_scenes = await dataset_has_scenes(self.db, ds.id)
        return {
            **_dataset_dict(ds),
            "project_count": pc,
            "total_size": int(total_size),
            "has_scenes": has_scenes,
        }

    async def create(
        self,
        name: str,
        description: str,
        data_type: str,
        user_id: uuid.UUID,
        *,
        axis_convention: str | None = None,
        is_temporal: bool = False,
    ) -> Dataset:
        ensure_dataset_storage_name_is_writable(name)
        ds_id = uuid.uuid4()
        display_id = await next_display_id(self.db, "datasets")
        kwargs: dict = {
            "id": ds_id,
            "display_id": display_id,
            "name": name,
            "description": description,
            "data_type": data_type,
            "is_temporal": is_temporal,
            "created_by": user_id,
        }
        # v0.13.11 · 仅当显式给定时写 metadata，否则走 server_default '{}'。
        if axis_convention is not None:
            kwargs["metadata_"] = {"axis_convention": axis_convention}
        ds = Dataset(**kwargs)
        self.db.add(ds)
        await self.db.flush()
        storage_service.ensure_bucket(storage_service.datasets_bucket)
        storage_service.create_folder(name, bucket=storage_service.datasets_bucket)
        return ds

    async def update(
        self,
        dataset_id: uuid.UUID,
        name: str | None,
        description: str | None,
        *,
        axis_convention=_UNSET,
    ) -> Dataset | None:
        ds = await self.db.get(Dataset, dataset_id)
        if not ds:
            return None
        if name is not None:
            ensure_dataset_storage_name_is_writable(name)
            ds.name = name
        if description is not None:
            ds.description = description
        # v0.13.11 · sentinel 区分「未传」/「显式 None」/「具体值」。
        # JSONB in-place mutation 不被 SQLAlchemy dirty-track，必须赋值整对象。
        if axis_convention is not _UNSET:
            meta = dict(ds.metadata_ or {})
            if axis_convention is None:
                meta.pop("axis_convention", None)
            else:
                meta["axis_convention"] = axis_convention
            ds.metadata_ = meta
        await self.db.flush()
        return ds

    async def delete(self, dataset_id: uuid.UUID) -> DatasetDeleteCleanup | None:
        ds = await self.db.get(Dataset, dataset_id)
        if not ds:
            return None
        from app.db.models.image_pyramid import ImagePyramidAsset

        asset_ids = list(
            (
                await self.db.execute(
                    select(ImagePyramidAsset.id)
                    .join(
                        DatasetItem,
                        DatasetItem.id == ImagePyramidAsset.dataset_item_id,
                    )
                    .where(DatasetItem.dataset_id == dataset_id)
                )
            )
            .scalars()
            .all()
        )
        await self.db.delete(ds)
        await self.db.flush()
        return DatasetDeleteCleanup(pyramid_asset_ids=tuple(asset_ids))

    @staticmethod
    def cleanup_deleted_storage(cleanup: DatasetDeleteCleanup) -> None:
        from app.services.image_pyramid import PYRAMID_PREFIX

        for asset_id in cleanup.pyramid_asset_ids:
            try:
                storage_service.delete_prefix(
                    f"{PYRAMID_PREFIX}/{asset_id}",
                    bucket=storage_service.media_cache_bucket,
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to clean image pyramid asset %s", asset_id)

    # ── Items ───────────────────────────────────────────────────────────────

    async def list_items(
        self,
        dataset_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        count_q = (
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == dataset_id)
        )
        total = (await self.db.execute(count_q)).scalar() or 0

        q = (
            select(DatasetItem)
            .where(DatasetItem.dataset_id == dataset_id)
            .order_by(DatasetItem.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(q)
        items = result.scalars().all()

        out = []
        for item in items:
            d = _item_dict(item)
            try:
                d["file_url"] = storage_service.generate_download_url(
                    item.file_path,
                    bucket=storage_service.datasets_bucket,
                )
            except Exception:
                d["file_url"] = None
            if item.thumbnail_path:
                try:
                    thumbnail_bucket = storage_service.bucket_for_cache_key(
                        item.thumbnail_path,
                        default=storage_service.datasets_bucket,
                    )
                    d["thumbnail_url"] = storage_service.generate_download_url(
                        item.thumbnail_path,
                        bucket=thumbnail_bucket,
                    )
                except Exception:
                    d["thumbnail_url"] = None
            out.append(d)
        return out, total

    async def find_by_hash(
        self, dataset_id: uuid.UUID, content_hash: str
    ) -> DatasetItem | None:
        result = await self.db.execute(
            select(DatasetItem).where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.content_hash == content_hash,
            )
        )
        return result.scalar_one_or_none()

    async def add_item(
        self,
        dataset_id: uuid.UUID,
        file_name: str,
        file_path: str,
        file_type: str,
        file_size: int | None = None,
        content_hash: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> DatasetItem:
        item = DatasetItem(
            dataset_id=dataset_id,
            file_name=file_name,
            file_path=file_path,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
            width=width,
            height=height,
        )
        self.db.add(item)

        ds = await self.db.get(Dataset, dataset_id)
        if ds:
            ds.file_count = (ds.file_count or 0) + 1

        await self.db.flush()
        return item

    async def delete_item(
        self, item_id: uuid.UUID, *, dataset_id: uuid.UUID | None = None
    ) -> bool:
        item = await self.db.get(DatasetItem, item_id)
        if not item or (dataset_id is not None and item.dataset_id != dataset_id):
            return False
        ds = await self.db.get(Dataset, item.dataset_id)
        if ds:
            ds.file_count = max((ds.file_count or 0) - 1, 0)
        from app.db.models.image_pyramid import ImagePyramidAsset
        from app.services.image_pyramid import PYRAMID_PREFIX

        asset_ids = list(
            (
                await self.db.execute(
                    select(ImagePyramidAsset.id).where(
                        ImagePyramidAsset.dataset_item_id == item.id
                    )
                )
            )
            .scalars()
            .all()
        )
        for asset_id in asset_ids:
            storage_service.delete_prefix(
                f"{PYRAMID_PREFIX}/{asset_id}",
                bucket=storage_service.media_cache_bucket,
            )
        await self.db.delete(item)
        await self.db.flush()
        return True

    async def ingest_one(
        self,
        dataset_id: uuid.UUID,
        relpath: str,
        stream: BinaryIO | None = None,
        *,
        size: int | None = None,
        storage_key: str | None = None,
        content_hash_hint: str | None = None,
        dest_relpath: str | None = None,
    ) -> IngestOutcome:
        """Ingest one source object into a dataset.

        `storage_key` is for objects already present in the datasets bucket (scan);
        otherwise `stream` is copied into that bucket in chunks while hashing.

        `dest_relpath` 给定时（连接器/扫描导入）保留源子目录结构作为 file_name 与
        storage_key 后缀（如 `a/img.jpg`）；不给时退回纯文件名（basename），其它调用零影响。
        """
        ds = await self.db.get(Dataset, dataset_id)
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
        final_name = await self._unique_file_name(dataset_id, source_name)
        base_for_ext = os.path.basename(final_name)
        ext = base_for_ext.rsplit(".", 1)[-1].lower() if "." in base_for_ext else ""
        file_type = _infer_file_type_from_ext(ext)
        uploaded_storage = False

        if storage_key:
            duplicate_path = await self._find_by_file_path(dataset_id, storage_key)
            if duplicate_path is not None:
                return IngestOutcome(
                    status="skipped",
                    relpath=relpath,
                    reason="file_path_exists",
                    item_id=duplicate_path.id,
                )
            content_hash = content_hash_hint if _is_md5(content_hash_hint) else None
            file_size = size
            head_bytes = b""
        else:
            if stream is None:
                return IngestOutcome(
                    status="error", relpath=relpath, error="stream missing"
                )
            storage_key = f"{ds.name}/{final_name}"
            content_type = (
                mimetypes.guess_type(final_name)[0] or "application/octet-stream"
            )
            content_hash, file_size, head_bytes = self._upload_stream_to_dataset_bucket(
                storage_key,
                stream,
                content_type=content_type,
            )
            uploaded_storage = True

        if content_hash:
            existing = await self.find_by_hash(dataset_id, content_hash)
            if existing is not None:
                if uploaded_storage:
                    try:
                        storage_service.delete_object(
                            storage_key, bucket=storage_service.datasets_bucket
                        )
                    except Exception:
                        pass
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
            if dims is None:
                dims = storage_service.read_image_dimensions(
                    storage_key,
                    bucket=storage_service.datasets_bucket,
                )
            if dims:
                width, height = dims

        item = await self.add_item(
            dataset_id=dataset_id,
            file_name=final_name,
            file_path=storage_key,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
            width=width,
            height=height,
        )
        linked_tasks = await self.create_tasks_for_items(dataset_id, [item.id])
        return IngestOutcome(
            status="added",
            relpath=relpath,
            item_id=item.id,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
            linked_tasks=linked_tasks,
        )

    async def enqueue_media_for_items(self, item_ids: list[uuid.UUID]) -> None:
        unique_item_ids = list(dict.fromkeys(item_ids))
        if not unique_item_ids:
            return
        rows = await self.db.execute(
            select(DatasetItem.id, DatasetItem.file_type).where(
                DatasetItem.id.in_(unique_item_ids)
            )
        )
        for item_id, file_type in rows.all():
            if file_type == "image":
                from app.workers.media import generate_thumbnail

                generate_thumbnail.delay(str(item_id))
            elif file_type == "video":
                from app.workers.media import generate_video_metadata

                generate_video_metadata.delay(str(item_id))

    async def _find_by_file_path(
        self, dataset_id: uuid.UUID, file_path: str
    ) -> DatasetItem | None:
        result = await self.db.execute(
            select(DatasetItem).where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.file_path == file_path,
            )
        )
        return result.scalar_one_or_none()

    async def _unique_file_name(self, dataset_id: uuid.UUID, file_name: str) -> str:
        existing_rows = await self.db.execute(
            select(DatasetItem.file_name).where(DatasetItem.dataset_id == dataset_id)
        )
        existing = {row[0] for row in existing_rows.all() if row[0]}
        if file_name not in existing:
            return file_name
        stem, ext = os.path.splitext(file_name)
        i = 1
        while f"{stem}-{i}{ext}" in existing:
            i += 1
        return f"{stem}-{i}{ext}"

    def _upload_stream_to_dataset_bucket(
        self,
        storage_key: str,
        stream: BinaryIO,
        *,
        content_type: str,
    ) -> tuple[str, int, bytes]:
        client = storage_service.client
        bucket = storage_service.datasets_bucket
        md5 = hashlib.md5()
        head = bytearray()
        total = 0
        part_number = 1
        parts: list[dict] = []
        upload_id: str | None = None

        resp = client.create_multipart_upload(
            Bucket=bucket,
            Key=storage_key,
            ContentType=content_type,
        )
        upload_id = resp["UploadId"]
        try:
            while True:
                chunk = stream.read(_STREAM_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                elif not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                total += len(chunk)
                md5.update(chunk)
                if len(head) < _IMAGE_HEAD_BYTES:
                    remaining = _IMAGE_HEAD_BYTES - len(head)
                    head.extend(chunk[:remaining])
                part = client.upload_part(
                    Bucket=bucket,
                    Key=storage_key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=chunk,
                )
                parts.append({"ETag": part["ETag"], "PartNumber": part_number})
                part_number += 1

            if parts:
                client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=storage_key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            else:
                client.abort_multipart_upload(
                    Bucket=bucket,
                    Key=storage_key,
                    UploadId=upload_id,
                )
                upload_id = None
                client.put_object(
                    Bucket=bucket,
                    Key=storage_key,
                    Body=b"",
                    ContentType=content_type,
                )
        except Exception:
            if upload_id is not None:
                try:
                    client.abort_multipart_upload(
                        Bucket=bucket,
                        Key=storage_key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass
            raise

        return md5.hexdigest(), total, bytes(head)

    # ── Scan & import from bucket ─────────────────────────────────────────

    async def scan_and_import(self, dataset_id: uuid.UUID) -> list[IngestOutcome]:
        ds = await self.db.get(Dataset, dataset_id)
        if not ds:
            return []
        ensure_dataset_storage_name_is_writable(ds.name)

        prefix = f"{ds.name}/"
        objects = storage_service.list_objects(
            prefix, bucket=storage_service.datasets_bucket
        )
        outcomes: list[IngestOutcome] = []
        for obj in objects:
            key: str = obj["key"]
            if key.endswith("/"):
                continue
            etag = obj.get("etag") or ""
            # 保留数据集前缀下的子目录层级作为 file_name（如 a/img.jpg）
            rel_under_ds = key[len(prefix) :] if key.startswith(prefix) else key
            outcomes.append(
                await self.ingest_one(
                    dataset_id,
                    key,
                    size=obj.get("size"),
                    storage_key=key,
                    content_hash_hint=etag if _is_md5(etag) else None,
                    dest_relpath=rel_under_ds,
                )
            )
        return outcomes

    # ── Project linking ─────────────────────────────────────────────────────

    async def link_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> LinkProjectResult:
        """v0.12.0 · 建 ProjectDataset link，并按 item 数决定同步 / 异步建 task。

        item 数 ≤ task_create_sync_threshold：同步分块建 task（沿用旧体验）。
        > 阈值：不在本请求里建 task，改为创建 CREATE_TASKS async_job 并由 endpoint
        在 db.commit() 之后 enqueue。返回 LinkProjectResult 让 endpoint 决策。
        """
        existing = (
            await self.db.execute(
                select(ProjectDataset).where(
                    ProjectDataset.dataset_id == dataset_id,
                    ProjectDataset.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return LinkProjectResult(link=existing)

        dataset = await self.db.get(Dataset, dataset_id)
        project = await self.db.get(Project, project_id)
        if dataset is None or project is None:
            raise HTTPException(status_code=404, detail="Dataset or project not found")
        has_scenes = await dataset_has_scenes(self.db, dataset_id)
        mismatch = kind_mismatch_detail(
            project_kind(project),
            dataset_kind(dataset, has_scenes=has_scenes),
        )
        if mismatch:
            raise HTTPException(status_code=422, detail=mismatch)

        link = ProjectDataset(dataset_id=dataset_id, project_id=project_id)
        self.db.add(link)
        await self.db.flush()

        item_count = (
            await self.db.execute(
                select(func.count())
                .select_from(DatasetItem)
                .where(DatasetItem.dataset_id == dataset_id)
            )
        ).scalar() or 0

        # v0.7.3：不再为新接入的 dataset 自建「默认包」batch。task 直接 batch_id=NULL，
        # 走「未归类任务」语义；BatchesSection 顶部横带提示，用户主动 split 才入 batch。
        # Resolve this decision at link-request time.  A queued CREATE_TASKS
        # job already has its branch chosen and must not change when an admin
        # later adjusts the threshold.
        task_create_sync_threshold = await SystemSettingsService.get(
            self.db, "task_create_sync_threshold"
        )
        if item_count > task_create_sync_threshold:
            job = await async_job_svc.create_job(
                self.db,
                kind=AsyncJobKind.CREATE_TASKS.value,
                project_id=project_id,
                payload={
                    "dataset_id": str(dataset_id),
                    "project_id": str(project_id),
                    "mode": "link",
                },
            )
            return LinkProjectResult(link=link, async_job_id=job.id)

        # 同步快路径：build_tasks_for_link 内部分块 commit（job_id=None 不上报进度）。
        result = await build_tasks_for_link(
            self.db, dataset_id=dataset_id, project_id=project_id
        )
        return LinkProjectResult(link=link, created_tasks=result["created"])

    async def assert_temporal_dataset_has_scenes(self, dataset_id: uuid.UUID) -> None:
        ds = await self.db.get(Dataset, dataset_id)
        if ds is None or not ds.is_temporal:
            return
        if not await dataset_has_scenes(self.db, dataset_id):
            raise ValueError("声明为时序数据集但未识别出任何 scene,请检查目录结构")

    async def create_tasks_for_items(
        self, dataset_id: uuid.UUID, item_ids: list[uuid.UUID]
    ) -> int:
        """Create tasks for newly added dataset items in already linked projects.

        `link_project` handles the initial backfill. This method covers the append path:
        upload / zip / scan can add new DatasetItem rows after a dataset is already linked.
        """
        unique_item_ids = list(dict.fromkeys(item_ids))
        if not unique_item_ids:
            return 0

        project_rows = await self.db.execute(
            select(ProjectDataset.project_id).where(
                ProjectDataset.dataset_id == dataset_id
            )
        )
        project_ids = [row[0] for row in project_rows.all()]
        if not project_ids:
            return 0

        items_result = await self.db.execute(
            select(
                DatasetItem.id,
                DatasetItem.file_name,
                DatasetItem.file_path,
                DatasetItem.file_type,
            ).where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.id.in_(unique_item_ids),
            )
        )
        items = items_result.all()
        if not items:
            return 0

        existing_rows = await self.db.execute(
            select(Task.project_id, Task.dataset_item_id).where(
                Task.project_id.in_(project_ids),
                Task.dataset_item_id.in_([item.id for item in items]),
            )
        )
        existing_pairs = {(row[0], row[1]) for row in existing_rows.all()}

        pending = []
        for project_id in project_ids:
            for item in items:
                if (project_id, item.id) in existing_pairs:
                    continue
                pending.append((project_id, item))

        if not pending:
            return 0

        # v0.12.0：按 5000 分块 INSERT，避免一条巨 INSERT；本路径仍同步（上游
        # upload/zip/scan 语义不变）。
        chunk_size = 5000
        total_created = 0
        for start in range(0, len(pending), chunk_size):
            chunk = pending[start : start + chunk_size]
            seq_result = await self.db.execute(
                text("SELECT nextval('display_seq_tasks') FROM generate_series(1, :n)"),
                {"n": len(chunk)},
            )
            display_nums = [row[0] for row in seq_result.all()]

            rows = [
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "dataset_item_id": item.id,
                    "batch_id": None,
                    "display_id": f"T-{display_nums[i]}",
                    "file_name": item.file_name,
                    "file_path": item.file_path,
                    "file_type": item.file_type,
                    "status": "pending",
                    "annotation_contributor_ids": [],
                }
                for i, (project_id, item) in enumerate(chunk)
            ]
            await self.db.execute(insert(Task), rows)
            total_created += len(chunk)

        created_by_project: dict[uuid.UUID, int] = {}
        for project_id, _item in pending:
            created_by_project[project_id] = created_by_project.get(project_id, 0) + 1

        projects = (
            (await self.db.execute(select(Project).where(Project.id.in_(project_ids))))
            .scalars()
            .all()
        )
        for project in projects:
            project.total_tasks = (project.total_tasks or 0) + created_by_project.get(
                project.id, 0
            )

        await self.db.flush()
        return total_created

    async def unlink_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> dict | None:
        """v0.6.7 二修 B-10：hard-unlink ——级联删除该 dataset 在该 project 下的所有 task
        (含 annotations / comments / locks)，不再保留为孤儿。

        理由：用户期望「取消关联 = 撤销 link 的全部副作用」，soft-unlink 留下进度永远停在历史值。
        相关数据丢失（annotations / 子项）通过前端二次确认 + 数字提示让用户知情。

        v0.7.3 fix：原实现只重算 batch 计数器，导致 link 自建的「默认包」/ 用户从该 dataset
        task 切出去的 batch 在 task 全删后变成空壳挂在列表里。现在：删 task 前记下「即将失去 task 的
        batch 集合」，重算后把 total_tasks==0 且非 B-DEFAULT 的批次也删掉。

        返回：None 表示 link 不存在；否则
            {"deleted_tasks": N, "deleted_annotations": M, "deleted_batches": K,
             "deleted_batch_ids": [...], "soft": false}
        """
        from app.db.models.annotation import Annotation
        from app.db.models.annotation_comment import AnnotationComment
        from app.db.models.task_lock import TaskLock

        # 1. 找出本次要删的 task ids 与所属 batch ids
        target_rows = (
            await self.db.execute(
                select(Task.id, Task.batch_id)
                .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                .where(
                    Task.project_id == project_id,
                    DatasetItem.dataset_id == dataset_id,
                )
            )
        ).all()
        target_task_ids: list[uuid.UUID] = [r[0] for r in target_rows]
        affected_batch_ids: set[uuid.UUID] = {
            r[1] for r in target_rows if r[1] is not None
        }

        # 2. 找出对应 annotation ids（用于 annotation_comments 级联）
        ann_ids: list[uuid.UUID] = []
        ann_count = 0
        if target_task_ids:
            ann_ids = list(
                (
                    await self.db.execute(
                        select(Annotation.id).where(
                            Annotation.task_id.in_(target_task_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            ann_count = len(ann_ids)

        # 3. 级联删除（顺序关键：先 child 后 parent）
        if ann_ids:
            await self.db.execute(
                delete(AnnotationComment).where(
                    AnnotationComment.annotation_id.in_(ann_ids)
                )
            )
        if target_task_ids:
            await self.db.execute(
                delete(Annotation).where(Annotation.task_id.in_(target_task_ids))
            )
            await self.db.execute(
                delete(TaskLock).where(TaskLock.task_id.in_(target_task_ids))
            )
            await self.db.execute(delete(Task).where(Task.id.in_(target_task_ids)))

        # 4. 删 ProjectDataset link
        result = await self.db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id == dataset_id,
                ProjectDataset.project_id == project_id,
            )
        )
        if result.rowcount == 0:
            return None

        # 5. 重算 project 计数器
        project = await self.db.get(Project, project_id)
        if project:
            row = (
                await self.db.execute(
                    select(
                        func.count().label("total"),
                        func.count()
                        .filter(Task.status == "completed")
                        .label("completed"),
                        func.count().filter(Task.status == "review").label("review"),
                    ).where(Task.project_id == project_id)
                )
            ).one()
            project.total_tasks = row.total
            project.completed_tasks = row.completed
            project.review_tasks = row.review

        # 6. 重算所有该 project 的 batch 计数器（被删 task 之前可能在某个 batch 里）
        batches = (
            (
                await self.db.execute(
                    select(TaskBatch).where(TaskBatch.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        for b in batches:
            r = (
                await self.db.execute(
                    select(
                        func.count().label("total"),
                        func.count()
                        .filter(Task.status == "completed")
                        .label("completed"),
                        func.count().filter(Task.status == "review").label("review"),
                    ).where(Task.batch_id == b.id)
                )
            ).one()
            b.total_tasks = r.total
            b.completed_tasks = r.completed
            b.review_tasks = r.review

        # 7. 级联清理：失去 task 后变空壳的 batch 删除（B-DEFAULT 永远保留）
        deleted_batch_ids: list[uuid.UUID] = []
        if affected_batch_ids:
            for b in batches:
                if (
                    b.id in affected_batch_ids
                    and b.total_tasks == 0
                    and b.display_id != "B-DEFAULT"
                ):
                    await self.db.execute(delete(TaskBatch).where(TaskBatch.id == b.id))
                    deleted_batch_ids.append(b.id)

        await self.db.flush()
        return {
            "deleted_tasks": len(target_task_ids),
            "deleted_annotations": ann_count,
            "deleted_batches": len(deleted_batch_ids),
            "deleted_batch_ids": [str(bid) for bid in deleted_batch_ids],
            "soft": False,
        }

    async def get_linked_projects(self, dataset_id: uuid.UUID) -> list[Project]:
        result = await self.db.execute(
            select(Project)
            .join(ProjectDataset, ProjectDataset.project_id == Project.id)
            .where(ProjectDataset.dataset_id == dataset_id)
        )
        return list(result.scalars().all())


_IMAGE_EXTS = {"jpg", "jpeg", "png", "bmp", "webp", "tiff", "tif", "gif", "svg"}
_VIDEO_EXTS = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm"}
# v0.13.0 · 点云联合标注地基 (0.13.0-3): 放开点云扩展名 → file_type=point_cloud。
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


def _is_md5(value: str | None) -> bool:
    return bool(value and len(value) == 32)


def _infer_file_type_from_ext(ext: str) -> str:
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _POINTCLOUD_EXTS:
        return "point_cloud"
    return "other"


def _dataset_dict(ds: Dataset) -> dict:
    return {
        "id": ds.id,
        "display_id": ds.display_id,
        "name": ds.name,
        "description": ds.description,
        "data_type": ds.data_type,
        "is_temporal": ds.is_temporal,
        "has_scenes": False,
        "file_count": ds.file_count,
        "total_size": 0,  # 调用方会覆盖
        "created_by": ds.created_by,
        # v0.13.11 · 派生自 metadata_["axis_convention"], None = 视作 iso_8855。
        "axis_convention": (ds.metadata_ or {}).get("axis_convention"),
        "created_at": ds.created_at,
        "updated_at": ds.updated_at,
    }


def _item_dict(item: DatasetItem) -> dict:
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "file_name": item.file_name,
        "file_path": item.file_path,
        "file_type": item.file_type,
        "file_size": item.file_size,
        "content_hash": item.content_hash,
        "width": item.width,
        "height": item.height,
        "blurhash": item.blurhash,
        "metadata": item.metadata_,
        "created_at": item.created_at,
    }
