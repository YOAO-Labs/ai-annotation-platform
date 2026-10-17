from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import mimetypes
import os
import uuid
from typing import Any, AsyncIterator, BinaryIO, Literal, Mapping, Sequence

from fastapi import HTTPException
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    ProjectDataset,
    Scene,
    VideoFrameIndex,
)
from app.db.models.project import Project
from app.services.display_id import next_display_id
from app.services.dataset_task_facts import PreparedDatasetItem
from app.services.project_kind import (
    dataset_has_scenes,
    dataset_kind,
    kind_mismatch_detail,
    media_kind_aliases,
    project_kind,
)
from app.services.media_job_dispatch import MediaJobDispatcher
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from app.services.active_learning_references import guard_source

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
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DatasetProjectAssociation:
    link: ProjectDataset
    existing: bool
    item_count: int


@dataclass(frozen=True)
class DatasetDeleteCleanup:
    pyramid_asset_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class NuScenesDatasetImport:
    dataset: Dataset
    storage_root: str
    needs_pcd_upgrade: bool


@dataclass(frozen=True)
class ResetDatasetSummary:
    id: uuid.UUID
    display_id: str
    name: str


class DatasetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_reset(self) -> tuple[ResetDatasetSummary, ...]:
        rows = (
            await self.db.execute(select(Dataset.id, Dataset.display_id, Dataset.name))
        ).all()
        return tuple(
            ResetDatasetSummary(id=row.id, display_id=row.display_id, name=row.name)
            for row in rows
        )

    async def reset_all(self) -> None:
        """Delete dataset rows and links only; object-store keys are left untouched."""
        await self.db.execute(delete(ProjectDataset))
        await self.db.execute(delete(DatasetItem))
        await self.db.execute(delete(Dataset))

    async def create_local_image_import_dataset(
        self, *, name: str, description: str, created_by: uuid.UUID
    ) -> Dataset:
        dataset_id = uuid.uuid4()
        dataset = Dataset(
            id=dataset_id,
            display_id=f"DS-{str(dataset_id)[:6].upper()}",
            name=name,
            description=description,
            data_type="image",
            created_by=created_by,
        )
        self.db.add(dataset)
        await self.db.flush()
        return dataset

    async def ensure_nuscenes_dataset(
        self,
        *,
        display_id: str,
        name: str,
        owner_id: uuid.UUID,
        version: str,
        scene_names: list[str],
        axis_convention: str,
        frame: Literal["sensor", "ego"],
        pcd_encoding: str,
        flush: bool = True,
    ) -> NuScenesDatasetImport:
        dataset = await self.db.scalar(
            select(Dataset).where(Dataset.display_id == display_id)
        )
        if dataset is None:
            dataset_id = uuid.uuid4()
            storage_root = f"{TRUSTED_NUSCENES_PREFIX}-{dataset_id.hex}"
            dataset = Dataset(
                id=dataset_id,
                display_id=display_id,
                name=name,
                data_type="point_cloud",
                is_temporal=True,
                created_by=owner_id,
                metadata_={
                    "axis_convention": axis_convention,
                    "source": {
                        "format": "nuscenes",
                        "version": version,
                        "scenes": sorted(scene_names),
                        "pcd_encoding": pcd_encoding,
                        "storage_root": storage_root,
                        "storage_layout": "uuid_v1",
                    },
                },
            )
            self.db.add(dataset)
            if flush:
                await self.db.flush()
            return NuScenesDatasetImport(
                dataset=dataset, storage_root=storage_root, needs_pcd_upgrade=False
            )

        metadata = dict(dataset.metadata_ or {})
        existing_source = metadata.get("source")
        stored_root = (
            str(existing_source.get("storage_root"))
            if isinstance(existing_source, dict) and existing_source.get("storage_root")
            else None
        )
        storage_layout = (
            str(existing_source.get("storage_layout"))
            if isinstance(existing_source, dict)
            and existing_source.get("storage_layout")
            else None
        )
        expected_root = f"{TRUSTED_NUSCENES_PREFIX}-{dataset.id.hex}"
        uses_legacy_root = stored_root is None or (
            storage_layout == "legacy_name" and stored_root == name
        )
        if (
            stored_root is not None
            and stored_root != expected_root
            and not uses_legacy_root
        ):
            raise ValueError("nuScenes dataset storage_root does not match its UUID")
        storage_root = stored_root or name
        if uses_legacy_root:
            conflicting_item = await self.db.scalar(
                select(DatasetItem.id)
                .where(DatasetItem.dataset_id != dataset.id)
                .where(DatasetItem.file_path.startswith(f"{storage_root}/"))
                .limit(1)
            )
            if conflicting_item is not None:
                raise ValueError(
                    f"legacy nuScenes storage root {storage_root!r} is shared by "
                    "another dataset; migrate it before re-import"
                )
        needs_pcd_upgrade = not isinstance(existing_source, dict) or (
            existing_source.get("pcd_encoding") != pcd_encoding
        )
        existing_axis = metadata.get("axis_convention")
        if existing_axis and existing_axis != axis_convention:
            raise ValueError(
                f"dataset {dataset.display_id} axis_convention={existing_axis!r} "
                f"cannot accept --frame {frame} ({axis_convention!r}); "
                "use a different --dataset-name to avoid mixing sensor and ego frames"
            )
        if not existing_axis:
            metadata["axis_convention"] = axis_convention
        metadata["source"] = {
            "format": "nuscenes",
            "version": version,
            "scenes": sorted(scene_names),
            "pcd_encoding": pcd_encoding,
            "storage_root": storage_root,
            "storage_layout": "legacy_name" if uses_legacy_root else "uuid_v1",
        }
        dataset.metadata_ = metadata
        dataset.is_temporal = True
        if flush:
            await self.db.flush()
        return NuScenesDatasetImport(
            dataset=dataset,
            storage_root=storage_root,
            needs_pcd_upgrade=needs_pcd_upgrade,
        )

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

    async def delete_dataset_record(self, ds: Dataset) -> DatasetDeleteCleanup:
        from app.db.models.image_pyramid import ImagePyramidAsset

        asset_ids = list(
            (
                await self.db.execute(
                    select(ImagePyramidAsset.id)
                    .join(
                        DatasetItem,
                        DatasetItem.id == ImagePyramidAsset.dataset_item_id,
                    )
                    .where(DatasetItem.dataset_id == ds.id)
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
        metadata_: dict | None = None,
        *,
        flush: bool = True,
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
        if metadata_ is not None:
            item.metadata_ = metadata_
        self.db.add(item)

        ds = await self.db.get(Dataset, dataset_id)
        if ds:
            ds.file_count = (ds.file_count or 0) + 1

        if flush:
            await self.db.flush()
        return item

    @staticmethod
    def set_nuscenes_export_context(item: DatasetItem, context: dict) -> None:
        metadata = dict(item.metadata_ or {})
        metadata["nuscenes_export"] = context
        item.metadata_ = metadata

    @staticmethod
    def backfill_camera_dimensions(
        item: DatasetItem, *, width: int, height: int
    ) -> bool:
        if item.width and item.height:
            return False
        item.width = width
        item.height = height
        return True

    @staticmethod
    def refresh_imported_item_content(
        item: DatasetItem, *, file_size: int, content_hash: str
    ) -> None:
        item.file_size = file_size
        item.content_hash = content_hash

    async def record_nuscenes_calibration_digests(self, dataset_id: uuid.UUID) -> None:
        from app.schemas._jsonb_types import SensorCalibration
        from app.services.sensor_calibration import calibration_digest

        camera_items = list(
            (
                await self.db.execute(
                    select(DatasetItem)
                    .where(DatasetItem.dataset_id == dataset_id)
                    .where(DatasetItem.file_type == "image")
                )
            ).scalars()
        )
        for item in camera_items:
            metadata = dict(item.metadata_ or {})
            source = metadata.get("nuscenes_export")
            calibration = metadata.get("calibration")
            if not isinstance(source, dict) or calibration is None:
                continue
            source = dict(source)
            source.setdefault(
                "platform_calibration_digest",
                calibration_digest(SensorCalibration.model_validate(calibration)),
            )
            metadata["nuscenes_export"] = source
            item.metadata_ = metadata

    async def recount_items(self, dataset_id: uuid.UUID) -> int:
        count = int(
            await self.db.scalar(
                select(func.count(DatasetItem.id)).where(
                    DatasetItem.dataset_id == dataset_id
                )
            )
            or 0
        )
        dataset = await self.db.get(Dataset, dataset_id)
        if dataset is not None:
            dataset.file_count = count
        await self.db.flush()
        return count

    async def delete_item(
        self, item_id: uuid.UUID, *, dataset_id: uuid.UUID | None = None
    ) -> bool:
        item = await self.db.scalar(
            select(DatasetItem).where(DatasetItem.id == item_id).with_for_update()
        )
        if not item or (dataset_id is not None and item.dataset_id != dataset_id):
            return False

        await guard_source(self.db, item_ids=[item_id])
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
        return IngestOutcome(
            status="added",
            relpath=relpath,
            item_id=item.id,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
        )

    async def enqueue_media_for_items(
        self, item_ids: list[uuid.UUID], *, dispatcher: MediaJobDispatcher
    ) -> None:
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
                dispatcher.dispatch_thumbnail(item_id)
            elif file_type == "video":
                dispatcher.dispatch_video_metadata(item_id)

    async def set_item_dimensions(
        self, item_id: uuid.UUID, dimensions: tuple[int, int]
    ) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is not None:
            item.width, item.height = dimensions

    async def record_thumbnail_error(self, item_id: uuid.UUID, error: str) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is not None:
            metadata = dict(item.metadata_ or {})
            metadata["thumbnail_error"] = error
            item.metadata_ = metadata

    async def set_item_thumbnail(
        self, item_id: uuid.UUID, thumbnail_path: str, *, blurhash: str
    ) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is not None:
            item.thumbnail_path = thumbnail_path
            item.blurhash = blurhash

    async def replace_video_frame_index(
        self,
        item_id: uuid.UUID,
        frame_timetable: Sequence[Mapping[str, Any]],
    ) -> None:
        await self.db.execute(
            delete(VideoFrameIndex).where(VideoFrameIndex.dataset_item_id == item_id)
        )
        self.db.add_all(
            [
                VideoFrameIndex(
                    dataset_item_id=item_id,
                    frame_index=entry["frame_index"],
                    pts_ms=entry["pts_ms"],
                    is_keyframe=entry["is_keyframe"],
                    pict_type=entry.get("pict_type"),
                    byte_offset=entry.get("byte_offset"),
                )
                for entry in frame_timetable
            ]
        )

    async def replace_video_frame_timetable(
        self,
        item: DatasetItem,
        frame_timetable: Sequence[Mapping[str, Any]],
        *,
        total_frames: int,
    ) -> None:
        await self.replace_video_frame_index(item.id, frame_timetable)
        metadata = dict(item.metadata_ or {})
        video_metadata = dict(metadata.get("video") or {})
        video_metadata["frame_timetable_frame_count"] = total_frames
        video_metadata.pop("frame_timetable_error", None)
        metadata["video"] = video_metadata
        item.metadata_ = metadata

    async def record_video_frame_timetable_error(
        self, item_id: uuid.UUID, error: str
    ) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is None:
            return
        metadata = dict(item.metadata_ or {})
        video_metadata = dict(metadata.get("video") or {})
        video_metadata["frame_timetable_error"] = error
        metadata["video"] = video_metadata
        item.metadata_ = metadata

    async def set_video_dimensions(
        self,
        item_id: uuid.UUID,
        *,
        width: Any | None,
        height: Any | None,
    ) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is None:
            return
        if width is not None:
            item.width = int(width)
        if height is not None:
            item.height = int(height)

    async def record_video_probe_error(
        self, item_id: uuid.UUID, video_metadata: Mapping[str, Any]
    ) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is not None:
            metadata = dict(item.metadata_ or {})
            metadata["video"] = dict(video_metadata)
            item.metadata_ = metadata

    async def save_video_materialization(
        self,
        item_id: uuid.UUID,
        video_metadata: Mapping[str, Any],
        *,
        thumbnail_path: str | None,
    ) -> None:
        item = await self.db.get(DatasetItem, item_id)
        if item is None:
            return
        metadata = dict(item.metadata_ or {})
        metadata["video"] = dict(video_metadata)
        item.metadata_ = metadata
        if thumbnail_path is not None:
            item.thumbnail_path = thumbnail_path

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

    async def iter_scan_and_import(
        self, dataset_id: uuid.UUID
    ) -> AsyncIterator[IngestOutcome]:
        ds = await self.db.get(Dataset, dataset_id)
        if ds is None:
            return
        ensure_dataset_storage_name_is_writable(ds.name)

        prefix = f"{ds.name}/"
        objects = storage_service.list_objects(
            prefix, bucket=storage_service.datasets_bucket
        )
        for obj in objects:
            key: str = obj["key"]
            if key.endswith("/"):
                continue
            etag = obj.get("etag") or ""
            rel_under_ds = key[len(prefix) :] if key.startswith(prefix) else key
            yield await self.ingest_one(
                dataset_id,
                key,
                size=obj.get("size"),
                storage_key=key,
                content_hash_hint=etag if _is_md5(etag) else None,
                dest_relpath=rel_under_ds,
            )

    # ── Project linking ─────────────────────────────────────────────────────

    async def associate_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> DatasetProjectAssociation:
        existing = (
            await self.db.execute(
                select(ProjectDataset).where(
                    ProjectDataset.dataset_id == dataset_id,
                    ProjectDataset.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return DatasetProjectAssociation(link=existing, existing=True, item_count=0)

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
        return DatasetProjectAssociation(
            link=link, existing=False, item_count=item_count
        )

    async def remove_project_association(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> bool:
        result = await self.db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id == dataset_id,
                ProjectDataset.project_id == project_id,
            )
        )
        return result.rowcount != 0

    async def linked_project_ids(self, dataset_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        rows = await self.db.execute(
            select(ProjectDataset.project_id).where(
                ProjectDataset.dataset_id == dataset_id
            )
        )
        return tuple(row[0] for row in rows.all())

    async def prepare_task_items(
        self,
        dataset_id: uuid.UUID,
        item_ids: tuple[uuid.UUID, ...],
    ) -> tuple[PreparedDatasetItem, ...]:
        if not item_ids:
            return ()
        query = select(
            DatasetItem.id,
            DatasetItem.file_name,
            DatasetItem.file_path,
            DatasetItem.file_type,
        ).where(
            DatasetItem.dataset_id == dataset_id,
            DatasetItem.id.in_(item_ids),
        )
        rows = (await self.db.execute(query)).all()
        return tuple(
            PreparedDatasetItem(
                id=row.id,
                file_name=row.file_name,
                file_path=row.file_path,
                file_type=row.file_type,
            )
            for row in rows
        )

    async def assert_temporal_dataset_has_scenes(self, dataset_id: uuid.UUID) -> None:
        ds = await self.db.get(Dataset, dataset_id)
        if ds is None or not ds.is_temporal:
            return
        if not await dataset_has_scenes(self.db, dataset_id):
            raise ValueError("声明为时序数据集但未识别出任何 scene,请检查目录结构")

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
