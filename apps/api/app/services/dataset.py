from __future__ import annotations

from dataclasses import dataclass
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
from app.services.dataset_assets import find_verified_content, register_item_asset
from app.services.dataset_storage_keys import (
    MANAGED_DATASET_PREFIX,
    managed_dataset_prefix,
    normalize_dataset_relative_path,
    dataset_relative_path,
    dataset_item_relative_path,
)
from app.services.project_kind import dataset_has_scenes
from aap_workspace.configuration_policy import (
    dataset_kind,
    kind_mismatch_detail,
    media_kind_aliases,
    project_kind,
)
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from app.services.system_settings_service import SystemSettingsService
from app.services.reference_keys import is_reference, PREFIX
from app.services.dataset_source_access import validate_dataset_write
from app.services.dataset_lifecycle import require_writable
from aap_assets_catalog.spatial_contract import (
    ProvenanceEntry,
    ReadItemsRequest,
    RecordCalibrationProvenanceRequest,
    canonical_calibration_document,
    normalize_calibration_record,
)
from app.services.assets_spatial import AssetsSpatialGateway
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
    if first_part.startswith(
        (
            TRUSTED_NUSCENES_PREFIX,
            MANAGED_DATASET_PREFIX,
            "__aap_upload_staging__",
            "__aap_auxiliary_originals__",
            PREFIX.rstrip("/"),
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="Dataset name uses a reserved storage namespace",
        )


@dataclass(frozen=True)
class ResetDatasetSummary:
    id: uuid.UUID
    display_id: str
    name: str


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
    directory_objects: tuple[tuple[str, str], ...] = ()


async def writable_dataset(db, dataset_id):
    """Preserve managed-writer serialization before validating lifecycle state."""
    await db.scalar(
        select(Dataset.id)
        .where(Dataset.id == dataset_id)
        .with_for_update(key_share=True)
    )
    return await require_writable(db, dataset_id)


async def build_tasks_for_link(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    chunk_size: int = 5000,
    directory_authority=None,
) -> dict:
    """v0.12.0 · 为 dataset link 分块建 task（worker / 同步快路径共用）。

    幂等：用 NOT EXISTS 过滤掉该 project 下已有 task 的 dataset_item，重复跑不双建。
    每块独立 commit（增量累加 project.total_tasks），传 job_id 时每块按 5% 粒度
    update_progress，供前端轮询看到进度。返回 {"created": N, "total": M}。

    v0.13.1 · 点云项目（project.data_type == "lidar"）分流到 scene 感知建任务器：
    先写各相机标定，再按帧建 Task + 多文件 link（见 services/pointcloud_import.py）。
    """
    await validate_dataset_write(
        db, dataset_id, directory_authority, project_id=project_id, lock=True
    )
    await db.execute(
        select(Dataset.id).where(Dataset.id == dataset_id).with_for_update()
    )
    project = await db.get(Project, project_id)
    if project and (project.data_type != "image" or project.scene_mode):
        if await db.scalar(
            select(DatasetItem.id)
            .where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.file_path.startswith(PREFIX, autoescape=True),
            )
            .limit(1)
        ):
            raise HTTPException(
                409, detail={"code": "reference_requires_image_project"}
            )
    if project and project.data_type == "lidar":
        from app.services.pointcloud_import import (
            attach_calibration,
            build_pointcloud_tasks_for_link,
        )

        await attach_calibration(db, dataset_id=dataset_id)
        # 标定独立落库：build_pointcloud 在「帧全已建过 task」时 total==0 早退且不 commit，
        # 若不在此 commit，attach_calibration 的 flush 会在 session 关闭时回滚 —— 重导入
        # （如修正 calib 文件后重跑）将刷不掉旧标定。commit 在这让标定持久化与建任务解耦。
        await validate_dataset_write(
            db, dataset_id, directory_authority, project_id=project_id, lock=True
        )
        await db.commit()
        return await build_pointcloud_tasks_for_link(
            db,
            dataset_id=dataset_id,
            project_id=project_id,
            job_id=job_id,
            chunk_size=chunk_size,
            directory_authority=directory_authority,
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
        await validate_dataset_write(
            db, dataset_id, directory_authority, project_id=project_id, lock=True
        )
        await db.execute(
            select(Dataset.id).where(Dataset.id == dataset_id).with_for_update()
        )
        present = set(
            await db.scalars(
                select(Task.dataset_item_id).where(
                    Task.project_id == project_id,
                    Task.dataset_item_id.in_([i.id for i in chunk]),
                )
            )
        )
        chunk = [i for i in chunk if i.id not in present]
        if not chunk:
            continue
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

        await validate_dataset_write(
            db, dataset_id, directory_authority, project_id=project_id, lock=True
        )
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
        location_id: uuid.UUID | None = None,
        actor=None,
        state: str = "active",
    ) -> tuple[list[dict], int]:
        q = select(Dataset)
        count_q = select(func.count()).select_from(Dataset)
        if state != "all":
            clause = (
                Dataset.archived_at.is_not(None)
                if state == "archived"
                else Dataset.archived_at.is_(None)
            )
            q, count_q = q.where(clause), count_q.where(clause)
        if location_id is not None:
            from app.services.dataset_workbench import location_dataset_scope

            if actor is None:
                raise HTTPException(403, "Location filtering requires an actor")
            clause = location_dataset_scope(location_id, actor)
            q = q.where(clause)
            count_q = count_q.where(clause)

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
            q.order_by(Dataset.created_at.desc(), Dataset.id)
            .limit(limit)
            .offset(offset)
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
            "storage_scan_prefix": managed_dataset_prefix(ds_id),
            "is_temporal": is_temporal,
            "created_by": user_id,
        }
        # v0.13.11 · 仅当显式给定时写 metadata，否则走 server_default '{}'。
        if axis_convention is not None:
            kwargs["metadata_"] = {"axis_convention": axis_convention}
        ds = Dataset(**kwargs)
        self.db.add(ds)
        await self.db.flush()
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
        ds = await writable_dataset(self.db, dataset_id)
        if name is not None:
            ensure_dataset_storage_name_is_writable(name)
            if ds.storage_scan_prefix is None:
                ds.storage_scan_prefix = f"{ds.name}/"
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
        ds = await self.db.get(
            Dataset, dataset_id, with_for_update=True, populate_existing=True
        )
        if not ds:
            return None
        await writable_dataset(self.db, dataset_id)
        from app.services.active_learning_references import guard_source

        await guard_source(self.db, dataset_id=dataset_id)
        from app.db.models.dataset_source import DatasetSourceItem, DatasetSourcePreview

        preview_rows = list(
            await self.db.scalars(
                select(DatasetSourcePreview)
                .join(
                    DatasetSourceItem,
                    (DatasetSourceItem.source_id == DatasetSourcePreview.source_id)
                    & (
                        DatasetSourceItem.path_digest
                        == DatasetSourcePreview.path_digest
                    ),
                )
                .where(
                    DatasetSourceItem.dataset_item_id.in_(
                        select(DatasetItem.id).where(
                            DatasetItem.dataset_id == dataset_id
                        )
                    ),
                    DatasetSourcePreview.object_key.is_not(None),
                )
            )
        )
        from app.services.directory_previews import retire_output

        for row in preview_rows:
            await retire_output(self.db, row.thumbnail_output_id)
        directory_objects = tuple(
            (row.bucket, row.object_key)
            for row in preview_rows
            if row.thumbnail_output_id is None
        )
        from app.services.managed_original_retirement import (
            capture_managed_original_retirements,
        )

        await capture_managed_original_retirements(self.db, dataset_id=dataset_id)
        await self.db.delete(ds)
        await self.db.flush()
        return DatasetDeleteCleanup(directory_objects=directory_objects)

    @staticmethod
    def cleanup_deleted_storage(cleanup: DatasetDeleteCleanup) -> None:
        for bucket, key in cleanup.directory_objects:
            if bucket != storage_service.media_cache_bucket or not key.startswith(
                "directory-previews/"
            ):
                continue
            try:
                storage_service.delete_object(key, bucket=bucket)
            except Exception:
                log.exception("failed to clean directory preview %s", key)

        # Independent pyramid owners survive the Dataset cascade for durable GC.

    # ── Items ───────────────────────────────────────────────────────────────

    async def list_items(
        self,
        dataset_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        item_ids: list[uuid.UUID] | None = None,
        actor_id: uuid.UUID | None = None,
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
            .order_by(DatasetItem.created_at.desc(), DatasetItem.id)
            .limit(limit)
            .offset(offset)
        )
        if item_ids is not None:
            q = q.where(DatasetItem.id.in_(item_ids))
        result = await self.db.execute(q)
        items = result.scalars().all()

        out = []
        for item in items:
            d = _item_dict(item)
            from app.services.directory_policy import owns_media

            directory_owned = await owns_media(self.db, item.id)
            if directory_owned:
                d["blurhash"] = None
                d["thumbnail_url"] = None
            if is_reference(item.file_path):
                d["file_url"] = None
                out.append(d)
                continue
            d["file_url"] = None
            if item.file_type == "video":
                from app.services import video_derivatives

                d["thumbnail_url"] = None
                d["blurhash"] = None
                if not directory_owned:
                    checked = await video_derivatives.thumbnail(
                        self.db, item_id=item.id
                    )
                    if checked is not None and checked[0] == item.thumbnail_path:
                        d["thumbnail_url"] = checked[1]
            elif item.thumbnail_path and not directory_owned:
                from app.services import image_pyramid_objects, thumbnail_outputs

                d["thumbnail_url"] = await image_pyramid_objects.thumbnail_url(
                    self.db, "dataset_item", item.id, item.thumbnail_path
                )
                if d["thumbnail_url"] is None:
                    d["thumbnail_url"] = await thumbnail_outputs.thumbnail_url(
                        self.db, "dataset_item", item.id, item.thumbnail_path
                    )
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
        sha256: str | None = None,
        object_etag: str | None = None,
        storage_binding=None,
        storage_ref=None,
        logical_path: str | None = None,
    ) -> DatasetItem:
        ds = await writable_dataset(self.db, dataset_id)
        item = DatasetItem(
            dataset_id=dataset_id,
            file_name=file_name,
            file_path=file_path,
            file_type=file_type,
            file_size=file_size,
            content_hash=content_hash,
            width=width,
            height=height,
            metadata_=(
                {"storage_relative_path": normalize_dataset_relative_path(logical_path)}
                if logical_path is not None
                else {}
            ),
        )
        self.db.add(item)

        if ds:
            ds.file_count = (ds.file_count or 0) + 1

        await self.db.flush()
        await register_item_asset(
            self.db,
            item,
            sha256=sha256,
            size_bytes=file_size,
            etag=object_etag,
            binding=storage_binding,
            storage_ref=storage_ref,
        )
        return item

    async def delete_item(
        self, item_id: uuid.UUID, *, dataset_id: uuid.UUID | None = None
    ) -> bool:
        candidate = await self.db.get(DatasetItem, item_id)
        if not candidate or (
            dataset_id is not None and candidate.dataset_id != dataset_id
        ):
            return False
        await writable_dataset(self.db, candidate.dataset_id)
        # Writers acquire the dataset before its items. NO KEY UPDATE also
        # remains compatible with existing FK references from other writers.
        await self.db.get(
            Dataset, candidate.dataset_id, with_for_update={"key_share": True}
        )
        item = await self.db.scalar(
            select(DatasetItem).where(DatasetItem.id == item_id).with_for_update()
        )
        if not item or (dataset_id is not None and item.dataset_id != dataset_id):
            return False
        from app.services.active_learning_references import guard_source

        await guard_source(self.db, item_ids=[item_id])
        ds = await self.db.get(Dataset, item.dataset_id)
        if ds:
            ds.file_count = max((ds.file_count or 0) - 1, 0)
        from app.db.models.dataset_source import DatasetSourceItem, DatasetSourcePreview

        previews = list(
            await self.db.scalars(
                select(DatasetSourcePreview)
                .join(
                    DatasetSourceItem,
                    (DatasetSourceItem.source_id == DatasetSourcePreview.source_id)
                    & (
                        DatasetSourceItem.path_digest
                        == DatasetSourcePreview.path_digest
                    ),
                )
                .where(
                    DatasetSourceItem.dataset_item_id == item.id,
                    DatasetSourcePreview.object_key.is_not(None),
                )
            )
        )
        from app.services.directory_previews import retire_output

        for row in previews:
            await retire_output(self.db, row.thumbnail_output_id)
        self._directory_delete_keys = getattr(self, "_directory_delete_keys", []) + [
            (row.bucket, row.object_key)
            for row in previews
            if row.thumbnail_output_id is None
        ]

        from app.services.managed_original_retirement import (
            capture_managed_original_retirements,
        )

        await capture_managed_original_retirements(
            self.db, dataset_id=item.dataset_id, item_id=item.id
        )
        await self.db.delete(item)
        await self.db.flush()
        return True

    async def cleanup_directory_previews(self):
        from app.services.directory_previews import delete_owned

        await delete_owned(getattr(self, "_directory_delete_keys", []))
        self._directory_delete_keys = []

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
        creation = None

        if storage_key and storage_key.lstrip("/").startswith(
            "__aap_auxiliary_originals__/"
        ):
            raise HTTPException(422, "Auxiliary originals cannot become dataset items")

        if storage_key:
            duplicate_path = await self._find_by_file_path(dataset_id, storage_key)
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
        if creation is not None and final_name in await self._existing_file_names(
            dataset_id
        ):
            from app.services.content_snapshot import ContentError

            raise ContentError("managed_original_creation_input_changed")
        item = await self.add_item(
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
        linked_tasks = await self.create_tasks_for_items(dataset_id, [item.id])
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

    async def enqueue_media_for_items(self, item_ids: list[uuid.UUID]) -> None:
        from app.services.reference_access import reject_reference_scope

        await reject_reference_scope(self.db, item_ids=item_ids)
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

                from app.services import video_derivative_originals as originals

                source = await originals.pin(self.db, "dataset_item", item_id)
                delegation = originals.system_authorization(
                    "dataset_item", item_id, source
                )
                generate_video_metadata.delay(str(item_id), delegation=delegation)

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

    async def _existing_file_names(self, dataset_id: uuid.UUID) -> set[str]:
        ds = await self.db.get(
            Dataset,
            dataset_id,
            with_for_update={"key_share": True},
            populate_existing=True,
        )
        existing_rows = await self.db.execute(
            select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
        )
        existing = set()
        for item in existing_rows.scalars():
            name, path = item.file_name, item.file_path
            existing.add(name)
            if (item.metadata_ or {}).get("storage_relative_path"):
                existing.add(dataset_item_relative_path(item, ds.name if ds else ""))
                continue
            if (
                ds
                and ds.storage_scan_prefix
                and path.startswith(ds.storage_scan_prefix)
                and not path.startswith(managed_dataset_prefix(dataset_id))
            ):
                existing.add(path[len(ds.storage_scan_prefix) :])
            else:
                existing.add(dataset_relative_path(path, ds.name if ds else ""))
        return existing

    async def _unique_file_name(
        self,
        dataset_id: uuid.UUID,
        file_name: str,
        *,
        existing_names: set[str] | None = None,
    ) -> str:
        file_name = normalize_dataset_relative_path(file_name)
        existing = (
            existing_names
            if existing_names is not None
            else await self._existing_file_names(dataset_id)
        )
        if file_name not in existing:
            return file_name
        stem, ext = os.path.splitext(file_name)
        i = 1
        while f"{stem}-{i}{ext}" in existing:
            i += 1
        return f"{stem}-{i}{ext}"

    # ── Scan & import from bucket ─────────────────────────────────────────

    async def scan_and_import(
        self, dataset_id: uuid.UUID, *, directory_authority=None
    ) -> list[IngestOutcome]:
        await validate_dataset_write(self.db, dataset_id, directory_authority)
        ds = await self.db.get(Dataset, dataset_id)
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

    # ── Project linking ─────────────────────────────────────────────────────

    async def link_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID, *, directory_authority=None
    ) -> LinkProjectResult:
        """v0.12.0 · 建 ProjectDataset link，并按 item 数决定同步 / 异步建 task。

        item 数 ≤ task_create_sync_threshold：同步分块建 task（沿用旧体验）。
        > 阈值：不在本请求里建 task，改为创建 CREATE_TASKS async_job 并由 endpoint
        在 db.commit() 之后 enqueue。返回 LinkProjectResult 让 endpoint 决策。
        """
        await validate_dataset_write(
            self.db, dataset_id, directory_authority, project_id=project_id, lock=True
        )
        dataset = await self.db.scalar(
            select(Dataset).where(Dataset.id == dataset_id).with_for_update()
        )
        await validate_dataset_write(
            self.db, dataset_id, directory_authority, project_id=project_id, lock=True
        )
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

        project = await self.db.get(Project, project_id)
        if dataset is None or project is None:
            raise HTTPException(status_code=404, detail="Dataset or project not found")
        has_scenes = await dataset_has_scenes(self.db, dataset_id)
        mismatch = kind_mismatch_detail(
            project_kind(project.data_type, project.scene_mode),
            dataset_kind(dataset.data_type, has_scenes=has_scenes),
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
                    "directory_authority": directory_authority,
                },
            )
            return LinkProjectResult(link=link, async_job_id=job.id)

        # 同步快路径：build_tasks_for_link 内部分块 commit（job_id=None 不上报进度）。
        result = await build_tasks_for_link(
            self.db,
            dataset_id=dataset_id,
            project_id=project_id,
            directory_authority=directory_authority,
        )
        return LinkProjectResult(link=link, created_tasks=result["created"])

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

    async def create_tasks_for_items(
        self, dataset_id: uuid.UUID, item_ids: list[uuid.UUID]
    ) -> int:
        """Create tasks for newly added dataset items in already linked projects.

        `link_project` handles the initial backfill. This method covers the append path:
        upload / zip / scan can add new DatasetItem rows after a dataset is already linked.
        """
        await writable_dataset(self.db, dataset_id)
        await self.db.execute(
            select(Dataset.id).where(Dataset.id == dataset_id).with_for_update()
        )
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
        if any(is_reference(item.file_path) for item in items):
            if await self.db.scalar(
                select(Project.id)
                .where(
                    Project.id.in_(project_ids),
                    (Project.data_type != "image") | Project.scene_mode.is_(True),
                )
                .limit(1)
            ):
                raise HTTPException(
                    409, detail={"code": "reference_requires_image_project"}
                )

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
        await writable_dataset(self.db, dataset_id)
        from app.db.models.annotation import Annotation
        from app.db.models.annotation_comment import AnnotationComment
        from app.db.models.annotation_feedback import AnnotationFeedback
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
                .order_by(Task.id)
                .with_for_update(of=Task)
            )
        ).all()
        target_task_ids: list[uuid.UUID] = [r[0] for r in target_rows]
        from app.services.active_learning_references import guard_source

        await guard_source(self.db, task_ids=target_task_ids)
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
            from app.services.comment_attachment_uploads import retire_for_comments

            await retire_for_comments(
                self.db,
                select(AnnotationComment.id).where(
                    AnnotationComment.annotation_id.in_(ann_ids)
                ),
            )
            await self.db.execute(
                delete(AnnotationComment).where(
                    AnnotationComment.annotation_id.in_(ann_ids)
                )
            )
        if target_task_ids:
            await self.db.execute(
                delete(AnnotationFeedback).where(
                    AnnotationFeedback.task_id.in_(target_task_ids)
                )
            )
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
        "archived_at": ds.archived_at,
        "archived_by": ds.archived_by,
        "lifecycle_revision": ds.lifecycle_revision,
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
        "original_identity_path": (
            f"/api/v1/datasets/{item.dataset_id}/items/{item.id}/original-identity"
            if not is_reference(item.file_path)
            else None
        ),
        "file_size": item.file_size,
        "content_hash": item.content_hash,
        "width": item.width,
        "height": item.height,
        "blurhash": item.blurhash,
        "metadata": item.metadata_,
        "created_at": item.created_at,
    }
