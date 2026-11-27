from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import uuid

from fastapi import HTTPException
from sqlalchemy import select, func, delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJobKind
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services import jobs_lifecycle_data as async_job_svc
from app.services.display_id import next_display_id
from app.services.dataset_assets import register_item_asset
from app.services.dataset_storage_keys import (
    MANAGED_DATASET_PREFIX,
    managed_dataset_prefix,
    normalize_dataset_relative_path,
    dataset_relative_path,
    dataset_item_relative_path,
)
from app.services.project_kind import dataset_has_scenes
from yoao_workspace.configuration_policy import (
    dataset_kind,
    kind_mismatch_detail,
    media_kind_aliases,
    project_kind,
)
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from app.services.system_settings_service import SystemSettingsService
from app.services.reference_keys import is_reference, PREFIX
from app.services.dataset_source_access import validate_dataset_write
from app.services.assets_dataset_data import dataset_metadata
from app.services.dataset_lifecycle import require_writable
from yoao_assets_catalog.spatial_contract import (
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
class LinkProjectResult:
    """link_project 返回结构：让 endpoint 拿到是否需要异步建 task。

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
        text("SELECT yoao_assets_v1.lock_dataset_reference(CAST(:id AS uuid))"),
        {"id": dataset_id},
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
    """为 dataset link 分块建 task（worker / 同步快路径共用）。

    幂等：用 NOT EXISTS 过滤掉该 project 下已有 task 的 dataset_item，重复跑不双建。
    每块独立 commit（增量累加 project.total_tasks），传 job_id 时每块按 5% 粒度
    update_progress，供前端轮询看到进度。返回 {"created": N, "total": M}。

    点云项目（project.data_type == "lidar"）分流到 scene 感知建任务器：
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
        if location_id is not None and actor is None:
            raise HTTPException(403, "Location filtering requires an actor")
        if self.db.autoflush:
            await self.db.flush()
        page = await self.db.scalar(
            text(
                "SELECT yoao_assets_v1.filtered_dataset_catalog("
                "CAST(:actor_id AS uuid), :search, CAST(:types AS text[]), "
                ":has_scenes, :state, CAST(:location_id AS uuid), "
                ":limit, CAST(:offset AS bigint))"
            ),
            {
                "actor_id": actor.id if actor else None,
                "search": search or None,
                "types": list(media_kind_aliases(data_type)) if data_type else None,
                "has_scenes": has_scenes,
                "state": state,
                "location_id": location_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return page["items"], page["total"]

    async def get(self, dataset_id: uuid.UUID):
        return await dataset_metadata(self.db, dataset_id)

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
        actor_id: uuid.UUID,
        operation_id: uuid.UUID | None = None,
        axis_convention=_UNSET,
    ) -> dict | None:
        from yoao_assets_catalog import DatasetPatch, UpdateDatasetRequest

        from app.services.assets_catalog import AssetsCatalogGateway
        from app.services.identity_account_lock_data import IdentityAccountLockData

        if await self.get(dataset_id) is None:
            return None
        await IdentityAccountLockData(self.db).lock_accounts(
            [actor_id], mode="key_share", nowait=False
        )
        ds = await writable_dataset(self.db, dataset_id)
        patch = {}
        if name is not None:
            ensure_dataset_storage_name_is_writable(name)
            if ds.storage_scan_prefix is None and ds.name != name:
                await self.db.execute(
                    text(
                        "SELECT yoao_assets_v1.pin_legacy_dataset_prefix("
                        "CAST(:id AS uuid), :revision)"
                    ),
                    {"id": ds.id, "revision": ds.lifecycle_revision},
                )
            patch["name"] = name
        if description is not None:
            patch["description"] = description
        if axis_convention is not _UNSET:
            patch["axis_convention"] = axis_convention
        result = await AssetsCatalogGateway(self.db).update(
            UpdateDatasetRequest(
                operation_id=operation_id or uuid.uuid4(),
                actor_id=actor_id,
                dataset_id=dataset_id,
                patch=DatasetPatch(**patch),
            ),
            commit=False,
        )
        return {
            **result.dataset.model_dump(),
            "archived_at": ds.archived_at,
            "archived_by": ds.archived_by,
            "lifecycle_revision": ds.lifecycle_revision,
        }

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
        directory_owned_media: bool = False,
    ):
        ds = await writable_dataset(self.db, dataset_id)
        item = (
            await self.db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.publish_managed_dataset_item("
                    "CAST(:id AS uuid), :revision, :file_name, :file_path, :file_type, "
                    "CAST(:size AS bigint), :content_hash, :width, :height, "
                    ":logical_path, :directory_owned_media)"
                ),
                {
                    "id": dataset_id,
                    "revision": ds.lifecycle_revision,
                    "file_name": file_name,
                    "file_path": file_path,
                    "file_type": file_type,
                    "size": file_size,
                    "content_hash": content_hash,
                    "width": width,
                    "height": height,
                    "logical_path": normalize_dataset_relative_path(logical_path)
                    if logical_path is not None
                    else None,
                    "directory_owned_media": directory_owned_media,
                },
            )
        ).one_or_none()
        if item is None:
            raise HTTPException(409, detail={"code": "dataset_lifecycle_changed"})
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

    async def _find_by_file_path(self, dataset_id: uuid.UUID, file_path: str):
        return (
            await self.db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.dataset_item_at_object_key("
                    "CAST(:id AS uuid), :key)"
                ),
                {"id": dataset_id, "key": file_path},
            )
        ).one_or_none()

    async def _existing_file_names(self, dataset_id: uuid.UUID) -> set[str]:
        await self.lock_dataset_reference(dataset_id)
        ds = await self.get(dataset_id)
        existing_rows = await self.db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.dataset_item_path_facts(CAST(:id AS uuid))"
            ),
            {"id": dataset_id},
        )
        existing = set()
        for item in existing_rows:
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

    async def link_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID, *, directory_authority=None
    ) -> LinkProjectResult:
        """建 ProjectDataset link，并按 item 数决定同步 / 异步建 task。

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

    async def associate_project(
        self, dataset_id: uuid.UUID, project_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> DatasetAssociationFacts:
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT yoao_assets_v1.associate_project("
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
                "SELECT yoao_assets_v1.lock_current_association("
                "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
            ),
            {"dataset_id": dataset_id, "project_id": project_id},
        )

    async def lock_dataset_reference(self, dataset_id: uuid.UUID) -> uuid.UUID | None:
        await self.db.flush()
        return await self.db.scalar(
            text("SELECT yoao_assets_v1.lock_dataset_reference(CAST(:id AS uuid))"),
            {"id": dataset_id},
        )

    async def assert_temporal_dataset_has_scenes(self, dataset_id: uuid.UUID) -> None:
        ds = await self.get(dataset_id)
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
        """Hard-unlink：级联删除该数据集在此项目下的所有任务
        (含 annotations / comments / locks)，不再保留为孤儿。

        理由：用户期望「取消关联 = 撤销 link 的全部副作用」，soft-unlink 留下进度永远停在历史值。
        相关数据丢失（annotations / 子项）通过前端二次确认 + 数字提示让用户知情。

        先记录将失去任务的批次，再重算计数；
        total_tasks==0 且非 B-DEFAULT 的受影响批次也会删除。

        返回：None 表示 link 不存在；否则
            {"deleted_tasks": N, "deleted_annotations": M, "deleted_batches": K,
             "deleted_batch_ids": [...], "soft": false}
        """
        await writable_dataset(self.db, dataset_id)
        from app.db.models.annotation import Annotation
        from app.db.models.annotation_comment import AnnotationComment
        from app.db.models.annotation_feedback import AnnotationFeedback
        from app.db.models.task_lock import TaskLock

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

        if ann_ids:
            from app.services.comment_attachment_uploads import retire_for_comments
            from app.services.labeling_discussion_relations import (
                annotation_comment_facts,
            )

            await retire_for_comments(
                self.db,
                list(
                    await self.db.scalars(
                        select(annotation_comment_facts.id).where(
                            annotation_comment_facts.annotation_id.in_(ann_ids)
                        )
                    )
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

        result = await self.db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id == dataset_id,
                ProjectDataset.project_id == project_id,
            )
        )
        if result.rowcount == 0:
            return None

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
