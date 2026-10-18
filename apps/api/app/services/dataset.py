from __future__ import annotations

from dataclasses import dataclass
import logging
import uuid
from typing import Any, Literal, Mapping, Sequence

from fastapi import HTTPException
from sqlalchemy import select, func, delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    ProjectDataset,
    Scene,
    VideoFrameIndex,
)
from app.services.display_id import next_display_id
from app.services.project_kind import (
    dataset_has_scenes,
    media_kind_aliases,
)
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from app.services.task_creation_contract import (
    DatasetAssociationFacts,
    decode_creation_reply,
)

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
        count = await self.db.scalar(
            update(Dataset)
            .where(Dataset.id == dataset_id)
            .values(file_count=Dataset.file_count + 1)
            .returning(Dataset.file_count)
            .execution_options(synchronize_session="fetch")
        )
        if count is None:
            raise HTTPException(404, "Dataset not found")
        cached = self.db.sync_session.identity_map.get(
            self.db.sync_session.identity_key(Dataset, dataset_id)
        )
        if cached is not None:
            set_committed_value(cached, "file_count", count)
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

    @staticmethod
    def record_uploaded_item_content(
        item: DatasetItem,
        *,
        file_size: int | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Stage independent upload observations without flushing or committing."""
        if file_size is not None:
            item.file_size = file_size
        if content_hash is not None:
            item.content_hash = content_hash

    async def record_nuscenes_calibration_digests(self, dataset_id: uuid.UUID) -> None:
        from aap_assets_catalog.spatial_values import (
            SensorCalibration,
            calibration_digest,
        )

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

    # ── Project linking ─────────────────────────────────────────────────────

    async def associate_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> DatasetAssociationFacts:
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT aap_assets_v1.associate_project("
                "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), "
                "CAST(:project_id AS uuid))"
            ),
            {"actor_id": actor_id, "dataset_id": dataset_id, "project_id": project_id},
        )
        return decode_creation_reply(
            payload, "aap-dataset-association/v1", DatasetAssociationFacts
        )

    async def lock_project_association(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> uuid.UUID | None:
        await self.db.flush()
        return await self.db.scalar(
            text(
                "SELECT aap_assets_v1.lock_current_association("
                "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
            ),
            {"dataset_id": dataset_id, "project_id": project_id},
        )

    async def lock_dataset_reference(self, dataset_id: uuid.UUID) -> uuid.UUID | None:
        await self.db.flush()
        return await self.db.scalar(
            text("SELECT aap_assets_v1.lock_dataset_reference(CAST(:id AS uuid))"),
            {"id": dataset_id},
        )

    async def assert_temporal_dataset_has_scenes(self, dataset_id: uuid.UUID) -> None:
        ds = await self.db.get(Dataset, dataset_id)
        if ds is None or not ds.is_temporal:
            return
        if not await dataset_has_scenes(self.db, dataset_id):
            raise ValueError("声明为时序数据集但未识别出任何 scene,请检查目录结构")


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
