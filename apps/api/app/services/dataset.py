from __future__ import annotations

from dataclasses import dataclass
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select, func, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import (
    Dataset,
    DatasetItem,
    ProjectDataset,
)
from aap_assets_catalog.spatial_contract import (
    ProvenanceEntry,
    ReadItemsRequest,
    RecordCalibrationProvenanceRequest,
    canonical_calibration_document,
    normalize_calibration_record,
)
from aap_assets_images.contract import ReadImageSourceRequest
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.display_id import next_display_id
from app.services.assets_media import browser_media_url
from app.services.image_source_references import image_source_references
from app.services.assets_spatial_views import spatial_item_references
from aap_workspace.configuration_policy import media_kind_aliases
from app.services.project_kind import dataset_has_scenes
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from app.services.task_creation_contract import (
    DatasetAssociationFacts,
    decode_creation_reply,
)

log = logging.getLogger(__name__)


def ensure_dataset_storage_name_is_writable(name: str) -> None:
    first_part = name.strip("/").split("/", 1)[0]
    if first_part.startswith(TRUSTED_NUSCENES_PREFIX):
        raise HTTPException(
            status_code=422,
            detail="Dataset name uses a reserved storage namespace",
        )


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
        has_scenes_map: dict[uuid.UUID, bool] = {}
        if has_scenes is None:
            total = (await self.db.execute(count_q)).scalar() or 0
            result = await self.db.execute(
                q.order_by(Dataset.created_at.desc()).limit(limit).offset(offset)
            )
            datasets = result.scalars().all()
        else:
            candidates = list(
                (await self.db.execute(q.order_by(Dataset.created_at.desc())))
                .scalars()
                .all()
            )
            for candidate in candidates:
                has_scenes_map[candidate.id] = await dataset_has_scenes(
                    self.db, candidate.id
                )
            matching = [
                candidate
                for candidate in candidates
                if has_scenes_map[candidate.id] is has_scenes
            ]
            total = len(matching)
            datasets = matching[offset : offset + limit]

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

            axis_rows = await self.db.execute(
                select(
                    spatial_item_references.c.dataset_id,
                    func.max(spatial_item_references.c.axis_convention),
                )
                .where(spatial_item_references.c.dataset_id.in_(ds_ids))
                .group_by(spatial_item_references.c.dataset_id)
            )
            axis_map = {row[0]: row[1] for row in axis_rows.all()}

            for ds_id in ds_ids:
                if ds_id not in has_scenes_map:
                    has_scenes_map[ds_id] = await dataset_has_scenes(self.db, ds_id)
        else:
            pc_map: dict = {}
            sz_map: dict = {}
            axis_map: dict = {}
            has_scenes_map = {}

        items = []
        for ds in datasets:
            items.append(
                {
                    **_dataset_dict(ds, axis_map.get(ds.id)),
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
        axis_convention = await self.db.scalar(
            select(func.max(spatial_item_references.c.axis_convention)).where(
                spatial_item_references.c.dataset_id == ds.id
            )
        )
        has_scenes = await dataset_has_scenes(self.db, ds.id)
        return {
            **_dataset_dict(ds, axis_convention),
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
    ) -> Dataset | None:
        ds = await self.db.get(Dataset, dataset_id)
        if not ds:
            return None
        if name is not None:
            ensure_dataset_storage_name_is_writable(name)
            ds.name = name
        if description is not None:
            ds.description = description
        await self.db.flush()
        return ds

    # ── Items ───────────────────────────────────────────────────────────────

    async def list_items(
        self,
        dataset_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        *,
        actor_id: uuid.UUID,
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
        image_items = [item for item in items if item.file_type == "image"]
        image_references_by_id = {}
        if image_items:
            references = await image_source_references(
                self.db,
                [
                    ReadImageSourceRequest(
                        actor_id=actor_id,
                        dataset_item_id=item.id,
                    )
                    for item in image_items
                ],
            )
            image_references_by_id = {
                item.id: reference
                for item, reference in zip(image_items, references, strict=True)
            }

        out = []
        for item in items:
            d = _item_dict(item)
            if item.file_type == "image":
                reference = image_references_by_id[item.id]
                source = reference.source if reference is not None else None
                matches_item = source is not None and (
                    source.owner.kind == "dataset_item"
                    and source.owner.id == item.id
                    and source.item_id == item.id
                    and source.dataset_id == item.dataset_id
                    and source.file_path == item.file_path
                    and source.file_size == item.file_size
                    and source.content_hash == item.content_hash
                )
                d["file_url"] = (
                    browser_media_url(reference.original_url) if matches_item else None
                )
                d["thumbnail_url"] = (
                    browser_media_url(reference.thumbnail_url) if matches_item else None
                )
                d["blurhash"] = None
                if matches_item:
                    d["width"] = source.width
                    d["height"] = source.height
                    d["blurhash"] = (
                        source.blurhash if source.thumbnail_current else None
                    )
            else:
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

    async def record_nuscenes_calibration_digests(self, dataset_id: uuid.UUID) -> None:
        gateway = AssetsSpatialGateway(self.db)
        items = await gateway.read_items(
            ReadItemsRequest(dataset_id=dataset_id), same_session_native=True
        )
        camera_ids = [
            item.id
            for item in items.items
            if item.file_type == "image"
            and isinstance(item.metadata.get("nuscenes_export"), dict)
            and item.metadata.get("calibration") is not None
        ]
        if not camera_ids:
            return
        records = await gateway.read_calibrations(camera_ids, same_session_native=True)
        entries = []
        for record in records:
            if record.error is not None:
                continue
            state = normalize_calibration_record(record)
            entries.append(
                ProvenanceEntry(
                    dataset_item_id=state.dataset_item_id,
                    calibration=state.calibration,
                    digest=state.digest,
                    canonical_document=canonical_calibration_document(
                        state.calibration
                    ),
                    source_snapshot=state.source_snapshot,
                )
            )
        for start in range(0, len(entries), 100):
            await gateway.record_calibration_provenance(
                RecordCalibrationProvenanceRequest(
                    operation_id=uuid.uuid4(),
                    dataset_id=dataset_id,
                    entries=entries[start : start + 100],
                )
            )

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


def _dataset_dict(ds: Dataset, axis_convention: str | None = None) -> dict:
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
        "axis_convention": axis_convention,
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
