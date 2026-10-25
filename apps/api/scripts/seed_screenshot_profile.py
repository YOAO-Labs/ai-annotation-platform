"""Desired-state ownership, rebuild, and task state for screenshot seed data."""

from __future__ import annotations

import copy
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient, make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.services.seed_membership_data import SeedMembershipWrites
from app.services.workspace_seed_data import (
    pin_seed_project_timestamps,
    register_project_counter_projection,
    register_seed_project_projection,
    seed_members,
    seed_projects,
)
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.identity_commands import IdentityCommandService
from app.application.project_deletion import (
    delete_project as delete_project_transaction,
)
from app.services.screenshot_seed_spec import (
    PROJECT_SPECS,
    SEED_MANAGED_BY,
    SEED_REVISION,
    USER_SPECS,
    resolve_project_spec,
)
from aap_assets_catalog.spatial_contract import SetDatasetAxisRequest
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.storage import storage_service
from aap_labeling.screenshot_operations_contract import (
    LinkSeedTasksToBatches,
    PinSeedTaskTimestamps,
    ReconcileSeedAnnotations,
    ReconcileSeedBatches,
    ReconcileSeedPredictions,
    ResetSeedTasks,
    SCREENSHOT_OPERATIONS_PROTOCOL,
    SeedAnnotation,
    SeedBatch,
    SeedBatchCounters,
    SeedTaskBatch,
    SeedTaskEvidence,
    SeedTaskReset,
    SeedTaskState,
    SetImageSeedTaskState,
    SyncSeedBatchCounters,
    SeedTaskTimestamp,
)
from app.services.labeling_core_read_relations import (
    annotation_row,
    batch_row,
    execute_core_read,
    prediction_row,
    prediction_meta_row,
    task_row,
)
from app.services.labeling_screenshot_operations import screenshot_operations_command

try:
    from seed_coco8 import COCO_NAMES, EXTRA_TOOL_BINDINGS
    from seed_ocr import OCR_TOOL_BINDINGS
    from seed_video import VIDEO_TOOL_BINDINGS
except ModuleNotFoundError:  # package import from tests
    from scripts.seed_coco8 import COCO_NAMES, EXTRA_TOOL_BINDINGS
    from scripts.seed_ocr import OCR_TOOL_BINDINGS
    from scripts.seed_video import VIDEO_TOOL_BINDINGS


logger = logging.getLogger(__name__)
FIXED_TIME = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
SCREENSHOT_MEDIA_WAIT_SECONDS = 600
ANNOTATION_NAMESPACE = uuid.UUID("6ebc9dd9-8b9d-4f5a-8658-843b3f0ac82d")
SCREENSHOT_POINTCLOUD_TOOL_BINDINGS = {
    "lidar_box_3d": {
        "classes": [{"name": "object", "order": 0}],
        "enabled": True,
        "attribute_schema": {"fields": []},
    },
    "point_mask_3d": {
        "classes": [
            {"name": "surface", "order": 0},
            {"name": "object", "order": 1},
        ],
        "enabled": True,
        "attribute_schema": {"fields": []},
    },
}


class ScreenshotSeedReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ScreenshotMediaItem:
    item_id: uuid.UUID
    file_name: str
    file_type: str


@dataclass(frozen=True)
class _ScreenshotMediaDataset:
    owner_id: uuid.UUID
    dataset_id: uuid.UUID
    display_id: str
    data_type: str
    items: tuple[_ScreenshotMediaItem, ...]


@dataclass(frozen=True)
class ScreenshotSeedPreparation:
    adopt_keys: frozenset[str]
    purged_projects: int = 0
    purged_datasets: int = 0


PROJECT_STATE = {
    "image_demo": {
        "name": "真实道路场景（截图）",
        "type_label": "图像 · 目标检测",
        "type_key": "image-det",
        "data_type": "image",
        "tool_bindings": {
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": name, "order": index}
                    for index, name in enumerate(COCO_NAMES)
                ],
            },
            **EXTRA_TOOL_BINDINGS,
        },
        "ai_enabled": True,
        "ai_interactive_enabled": True,
        "raster_mask_native_editing_enabled": True,
        "owner": "project_admin",
    },
    "video_demo": {
        "name": "真实城市交通追踪（截图）",
        "type_label": "视频 · 时序追踪",
        "type_key": "video-track",
        "data_type": "video",
        "tool_bindings": VIDEO_TOOL_BINDINGS,
        "ai_enabled": True,
        "ai_interactive_enabled": True,
        "owner": "project_admin",
    },
    "pointcloud_demo": {
        "name": "nuScenes mini 自动驾驶场景（截图）",
        "type_label": "点云检测",
        "type_key": "lidar",
        "data_type": "lidar",
        "tool_bindings": SCREENSHOT_POINTCLOUD_TOOL_BINDINGS,
        "ai_enabled": False,
        "ai_interactive_enabled": False,
        "owner": "admin",
    },
    "pointcloud_multicam_demo": {
        "name": "nuScenes 六相机环视（截图）",
        "type_label": "点云检测",
        "type_key": "lidar",
        "data_type": "lidar",
        "tool_bindings": SCREENSHOT_POINTCLOUD_TOOL_BINDINGS,
        "ai_enabled": False,
        "ai_interactive_enabled": False,
        "owner": "admin",
    },
    "ocr_demo": {
        "name": "OCR 文本识别（截图）",
        "type_label": "图像 · OCR",
        "type_key": "image-ocr",
        "data_type": "image",
        "tool_bindings": OCR_TOOL_BINDINGS,
        "ai_enabled": True,
        "ai_interactive_enabled": False,
        "owner": "project_admin",
    },
}

DATASET_STATE = {
    "image_demo": {
        "name": "screenshots-real-road-images",
        "description": "Wikimedia Commons CC0 奥克兰真实车流照片的确定性裁剪",
        "data_type": "image",
    },
    "video_demo": {
        "name": "screenshots-real-traffic-video",
        "description": "Wikimedia Commons 真实城市交通视频的确定性片段",
        "data_type": "video",
    },
    "pointcloud_demo": {
        "name": "nuscenes-mini",
        "description": "nuScenes mini scene-0061 的 39 帧激光雷达与六相机同步序列",
        "data_type": "point_cloud",
        "axis_convention": "iso_8855",
    },
    "pointcloud_multicam_demo": {
        "name": "screenshots-nuscenes-multicamera",
        "description": "nuScenes 开源六相机环视与激光雷达同步示例夹具",
        "data_type": "point_cloud",
        "axis_convention": "apollo",
    },
    "ocr_demo": {
        "name": "screenshots-ocr",
        "description": "RapidOCR 官方示例图截图夹具",
        "data_type": "image",
    },
}

LEGACY_PROJECT_NAMES = {
    "image_demo": {
        "COCO8 图片检测 (dev)",
        "合成道路场景（截图）",
        PROJECT_STATE["image_demo"]["name"],
    },
    "video_demo": {
        "行车视频跟踪 (dev)",
        "合成道路视频追踪（截图）",
        PROJECT_STATE["video_demo"]["name"],
    },
    "pointcloud_demo": {
        "点云联合标注 (dev)",
        "合成点云联合标注（截图）",
        "真实室内点云（截图）",
        "nuScenes mini 自动驾驶场景（dev）",
        PROJECT_STATE["pointcloud_demo"]["name"],
    },
    "pointcloud_multicam_demo": {PROJECT_STATE["pointcloud_multicam_demo"]["name"]},
    "ocr_demo": {"OCR 文本识别 (dev)", PROJECT_STATE["ocr_demo"]["name"]},
}
LEGACY_DATASET_NAMES = {
    "image_demo": {
        "coco8-dev",
        "screenshots-synthetic-images",
        DATASET_STATE["image_demo"]["name"],
    },
    "video_demo": {
        "tracking-car-dev",
        "screenshots-synthetic-video",
        DATASET_STATE["video_demo"]["name"],
    },
    "pointcloud_demo": {
        "pc-scene-dev",
        "screenshots-synthetic-pointcloud",
        "screenshots-real-pointcloud",
        DATASET_STATE["pointcloud_demo"]["name"],
    },
    "pointcloud_multicam_demo": {
        "pc-multicam-dev",
        DATASET_STATE["pointcloud_multicam_demo"]["name"],
    },
    "ocr_demo": {"ocr-dev", DATASET_STATE["ocr_demo"]["name"]},
}
LEGACY_STORAGE_PREFIXES = {
    "pointcloud_demo": {"pc-scene-dev/", "nuscenes-mini/"},
}


def _seed_marker(dataset: Dataset) -> dict:
    marker = (dataset.metadata_ or {}).get("seed")
    return marker if isinstance(marker, dict) else {}


async def _seed_users(db: AsyncSession) -> dict[str, uuid.UUID]:
    users: dict[str, uuid.UUID] = {}
    for key, (email, role) in USER_SPECS.items():
        rows = await IdentityCommandService(db).lookup_seed_account_facts(email)
        if len(rows) != 1 or rows[0][2] != role:
            raise ScreenshotSeedReconcileError(
                f"seed user {email} with role {role} is required"
            )
        users[key] = rows[0][0]
    return users


async def _assert_rebuild_ownership(
    db: AsyncSession,
    logical_key: str,
    project: Project | None,
    dataset: Dataset | None,
    users: dict[str, uuid.UUID],
) -> None:
    spec = PROJECT_SPECS[logical_key]
    marker = _seed_marker(dataset) if dataset is not None else {}
    if (
        marker.get("managed_by") == SEED_MANAGED_BY
        and marker.get("logical_key") == logical_key
    ):
        return

    if project is not None:
        state = PROJECT_STATE[logical_key]
        owner = users[state["owner"]]
        if (
            project.name not in LEGACY_PROJECT_NAMES[logical_key]
            or project.owner_id != owner
        ):
            raise ScreenshotSeedReconcileError(
                f"{spec.display_id}: fixed id is not an owned screenshot/demo seed project"
            )
        project_links = list(
            (
                await db.execute(
                    select(ProjectDataset).where(
                        ProjectDataset.project_id == project.id
                    )
                )
            ).scalars()
        )
        expected_dataset_ids = {dataset.id} if dataset is not None else set()
        if {link.dataset_id for link in project_links} != expected_dataset_ids:
            raise ScreenshotSeedReconcileError(
                f"{spec.display_id}: legacy seed has unexpected dataset links"
            )

    if dataset is not None:
        state = DATASET_STATE[logical_key]
        allowed_creators = {users["project_admin"], users["admin"]}
        if (
            dataset.name not in LEGACY_DATASET_NAMES[logical_key]
            or dataset.created_by not in allowed_creators
        ):
            raise ScreenshotSeedReconcileError(
                f"{spec.dataset_display_id}: fixed id is not an owned screenshot/demo seed dataset"
            )
        dataset_links = list(
            (
                await db.execute(
                    select(ProjectDataset).where(
                        ProjectDataset.dataset_id == dataset.id
                    )
                )
            ).scalars()
        )
        expected_project_ids = {project.id} if project is not None else set()
        if {link.project_id for link in dataset_links} != expected_project_ids:
            raise ScreenshotSeedReconcileError(
                f"{spec.dataset_display_id}: legacy seed is linked to unexpected projects"
            )
        items = list(
            (
                await db.execute(
                    select(DatasetItem).where(DatasetItem.dataset_id == dataset.id)
                )
            ).scalars()
        )
        resolved_spec = resolve_project_spec(spec, dataset.id, dataset.metadata_)
        allowed_prefixes = LEGACY_STORAGE_PREFIXES.get(
            logical_key, {spec.storage_prefix}
        ) | {resolved_spec.storage_prefix}
        if any(
            not any(item.file_path.startswith(prefix) for prefix in allowed_prefixes)
            for item in items
        ):
            raise ScreenshotSeedReconcileError(
                f"{spec.dataset_display_id}: legacy seed has unexpected storage paths"
            )


async def prepare_screenshot_seed(
    db: AsyncSession, *, repair: bool
) -> ScreenshotSeedPreparation:
    users = await _seed_users(db)
    existing: dict[str, tuple[Project | None, Dataset | None]] = {}
    adopt_keys: set[str] = set()
    for logical_key, spec in PROJECT_SPECS.items():
        project = await db.scalar(
            select(seed_projects).where(seed_projects.display_id == spec.display_id)
        )
        dataset = await db.scalar(
            select(Dataset).where(Dataset.display_id == spec.dataset_display_id)
        )
        existing[logical_key] = (project, dataset)
        if project is None and dataset is None:
            adopt_keys.add(logical_key)
        elif (project is None) != (dataset is None) and not repair:
            raise ScreenshotSeedReconcileError(
                f"{logical_key}: partial screenshot seed exists; rerun with --repair"
            )

    if not repair:
        return ScreenshotSeedPreparation(adopt_keys=frozenset(adopt_keys))

    storage_keys: set[str] = set()
    purged_projects = 0
    purged_datasets = 0
    for logical_key, (project, dataset) in existing.items():
        if project is None and dataset is None:
            continue
        await _assert_rebuild_ownership(db, logical_key, project, dataset, users)
        if dataset is not None:
            item_rows = await db.execute(
                select(DatasetItem.file_path).where(
                    DatasetItem.dataset_id == dataset.id
                )
            )
            for (file_path,) in item_rows.all():
                if file_path:
                    storage_keys.add(file_path)
        if project is not None:
            await delete_project_transaction(
                db, project.id, actor_id=users[PROJECT_STATE[logical_key]["owner"]]
            )
            purged_projects += 1
        if dataset is not None:
            await db.delete(dataset)
            await db.flush()
            purged_datasets += 1
        adopt_keys.add(logical_key)
    await db.commit()

    for key in storage_keys:
        try:
            storage_service.delete_object(key, bucket=storage_service.datasets_bucket)
        except Exception as exc:  # noqa: BLE001 - stale dev objects must not abort DB repair
            logger.warning(
                "cannot delete stale screenshot seed object %s: %s", key, exc
            )
    return ScreenshotSeedPreparation(
        adopt_keys=frozenset(adopt_keys),
        purged_projects=purged_projects,
        purged_datasets=purged_datasets,
    )


async def _resolve_owned_resources(
    db: AsyncSession,
    logical_key: str,
    *,
    adopt_keys: frozenset[str],
    asset_sha256: str,
) -> tuple[Project, Dataset, dict[str, Task]]:
    spec = PROJECT_SPECS[logical_key]
    projects = list(
        (
            await db.execute(
                select(seed_projects).where(seed_projects.display_id == spec.display_id)
            )
        ).scalars()
    )
    datasets = list(
        (
            await db.execute(
                select(Dataset).where(Dataset.display_id == spec.dataset_display_id)
            )
        ).scalars()
    )
    if len(projects) != 1 or len(datasets) != 1:
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: project/dataset is missing or duplicated"
        )
    project, dataset = projects[0], datasets[0]
    spec = resolve_project_spec(spec, dataset.id, dataset.metadata_)
    project_links = list(
        (
            await db.execute(
                select(ProjectDataset).where(ProjectDataset.project_id == project.id)
            )
        ).scalars()
    )
    dataset_links = list(
        (
            await db.execute(
                select(ProjectDataset).where(ProjectDataset.dataset_id == dataset.id)
            )
        ).scalars()
    )
    if (
        len(project_links) != 1
        or project_links[0].dataset_id != dataset.id
        or len(dataset_links) != 1
        or dataset_links[0].project_id != project.id
    ):
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: screenshot seed must have one exclusive project/dataset link"
        )

    marker = _seed_marker(dataset)
    if not marker and logical_key not in adopt_keys:
        raise ScreenshotSeedReconcileError(
            f"{spec.dataset_display_id}: unmarked fixed id collision; rerun with --repair"
        )
    if marker and (
        marker.get("managed_by") != SEED_MANAGED_BY
        or marker.get("profile") != "screenshots"
        or marker.get("logical_key") != logical_key
    ):
        raise ScreenshotSeedReconcileError(
            f"{spec.dataset_display_id}: seed ownership marker does not match"
        )
    if marker and (
        marker.get("revision") != SEED_REVISION
        or marker.get("asset_sha256") != asset_sha256
    ):
        raise ScreenshotSeedReconcileError(
            f"{spec.dataset_display_id}: screenshot seed revision or asset digest is stale; "
            "rerun with --repair"
        )
    dataset.metadata_ = {
        **(dataset.metadata_ or {}),
        "seed": {
            "managed_by": SEED_MANAGED_BY,
            "profile": "screenshots",
            "logical_key": logical_key,
            "revision": SEED_REVISION,
            "asset_sha256": asset_sha256,
        },
    }

    tasks: dict[str, Task] = {}
    managed_item_ids: set[uuid.UUID] = set()
    for task_spec in spec.tasks:
        items = list(
            (
                await db.execute(
                    select(DatasetItem).where(
                        DatasetItem.dataset_id == dataset.id,
                        DatasetItem.file_path == task_spec.file_path,
                    )
                )
            ).scalars()
        )
        if len(items) != 1 or not items[0].content_hash:
            raise ScreenshotSeedReconcileError(
                f"{logical_key}.{task_spec.key}: expected one hashed dataset item"
            )
        item = items[0]
        managed_item_ids.add(item.id)
        task_rows = list(
            (
                await execute_core_read(
                    db,
                    select(task_row).where(
                        task_row.project_id == project.id,
                        task_row.dataset_item_id == item.id,
                    ),
                )
            ).scalars()
        )
        if len(task_rows) != 1:
            raise ScreenshotSeedReconcileError(
                f"{logical_key}.{task_spec.key}: expected one linked task"
            )
        tasks[task_spec.key] = task_rows[0]

    linked_task_item_ids = set(
        (
            await execute_core_read(
                db,
                select(task_row.dataset_item_id)
                .join(DatasetItem, DatasetItem.id == task_row.dataset_item_id)
                .where(
                    task_row.project_id == project.id,
                    DatasetItem.dataset_id == dataset.id,
                ),
            )
        ).scalars()
    )
    if linked_task_item_ids != managed_item_ids:
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: managed dataset contains unexpected or missing tasks"
        )
    return project, dataset, tasks


async def _reconcile_members(
    db: AsyncSession, project: Project, users: dict[str, uuid.UUID]
) -> SeedMembershipWrites:
    assigned_by = users["project_admin"]
    writes = SeedMembershipWrites(db)
    for user_key, role in (("annotator", "annotator"), ("reviewer", "reviewer")):
        user = users[user_key]
        await writes.flush_before_read()
        member = await db.scalar(
            select(seed_members).where(
                seed_members.project_id == project.id,
                seed_members.user_id == user,
            )
        )
        if member is None:
            writes.add(
                project_id=project.id,
                user_id=user,
                role=role,
                assigned_by=assigned_by,
                assigned_at=FIXED_TIME,
            )
        else:
            writes.change(
                member, role=role, assigned_by=assigned_by, assigned_at=FIXED_TIME
            )
    return writes


async def _reconcile_batches(
    db: AsyncSession,
    project: Project,
    dataset: Dataset,
    tasks: dict[str, Task],
    users: dict[str, uuid.UUID],
    membership_writes: SeedMembershipWrites,
) -> dict[str, TaskBatch]:
    spec = PROJECT_SPECS["image_demo"]
    expected_display_ids = {batch_spec.display_id for batch_spec in spec.batches}
    existing_display_ids = set(
        (
            await execute_core_read(
                db,
                select(batch_row.display_id).where(batch_row.project_id == project.id),
            )
        ).scalars()
    )
    unexpected = sorted(existing_display_ids - expected_display_ids)
    if unexpected:
        raise ScreenshotSeedReconcileError(
            f"{project.display_id}: unexpected batches {unexpected}; rerun with --repair"
        )
    batches: dict[str, TaskBatch] = {}
    batch_requests = []
    for batch_spec in spec.batches:
        rows = list(
            (
                await execute_core_read(
                    db,
                    select(batch_row).where(
                        batch_row.project_id == project.id,
                        batch_row.display_id == batch_spec.display_id,
                    ),
                )
            ).scalars()
        )
        if len(rows) > 1:
            raise ScreenshotSeedReconcileError(
                f"{project.display_id}: duplicate batch {batch_spec.display_id}"
            )
        annotator = (
            users.get(batch_spec.annotator_key) if batch_spec.annotator_key else None
        )
        reviewer = (
            users.get(batch_spec.reviewer_key) if batch_spec.reviewer_key else None
        )
        batch = (
            rows[0]
            if rows
            else TaskBatch(
                id=uuid.uuid4(), project_id=project.id, display_id=batch_spec.display_id
            )
        )
        assigned_user_ids = tuple(
            str(user) for user in (annotator, reviewer) if user is not None
        )
        batch_requests.append(
            SeedBatch(
                id=batch.id,
                existing=bool(rows),
                display_id=batch_spec.display_id,
                name=batch_spec.name,
                status=batch_spec.status,
                annotator_id=annotator,
                reviewer_id=reviewer,
                assigned_user_ids=assigned_user_ids,
            )
        )
        batches[batch_spec.key] = batch
    await screenshot_operations_command(
        db,
        ReconcileSeedBatches(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            operation="reconcile_seed_batches",
            project_id=project.id,
            dataset_id=dataset.id,
            created_by=users["project_admin"],
            fixed_time=FIXED_TIME,
            batches=tuple(batch_requests),
        ),
    )
    for batch_spec in spec.batches:
        batch = batches[batch_spec.key]
        annotator = (
            users.get(batch_spec.annotator_key) if batch_spec.annotator_key else None
        )
        reviewer = (
            users.get(batch_spec.reviewer_key) if batch_spec.reviewer_key else None
        )
        set_committed_value(batch, "dataset_id", dataset.id)
        set_committed_value(batch, "name", batch_spec.name)
        set_committed_value(batch, "description", "screenshots seed managed batch")
        set_committed_value(batch, "status", batch_spec.status)
        set_committed_value(batch, "priority", 50)
        set_committed_value(batch, "annotator_id", annotator)
        set_committed_value(batch, "reviewer_id", reviewer)
        set_committed_value(
            batch,
            "assigned_user_ids",
            [str(value) for value in (annotator, reviewer) if value is not None],
        )
        set_committed_value(batch, "created_by", users["project_admin"])
        set_committed_value(batch, "created_at", FIXED_TIME)
        set_committed_value(batch, "updated_at", FIXED_TIME)
    await membership_writes.flush()

    task_specs = {task_spec.key: task_spec for task_spec in spec.tasks}
    task_links = []
    for key, task in tasks.items():
        task_links.append(
            SeedTaskBatch(
                task_id=task.id,
                batch_id=batches[task_specs[key].batch_key].id,
            )
        )
    await screenshot_operations_command(
        db,
        LinkSeedTasksToBatches(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            operation="link_seed_tasks_to_batches",
            project_id=project.id,
            tasks=tuple(task_links),
        ),
    )
    for link in task_links:
        task = next(value for value in tasks.values() if value.id == link.task_id)
        set_committed_value(task, "batch_id", link.batch_id)
    return batches


async def _reconcile_predictions(
    db: AsyncSession, project: Project, tasks: dict[str, Task]
) -> None:
    model_version = f"screenshot-seed:{SEED_REVISION}"
    managed_rows = (
        await execute_core_read(
            db,
            select(
                prediction_row.id,
                prediction_row.task_id,
                prediction_row.created_at,
            ).where(
                prediction_row.project_id == project.id,
                prediction_row.model_version == model_version,
            ),
        )
    ).all()
    predicted_task = tasks["predicted"]
    keep = [row for row in managed_rows if row.task_id == predicted_task.id]
    if len(keep) != 1:
        raise ScreenshotSeedReconcileError(
            "image_demo.predicted: expected one managed imported prediction"
        )
    remove = [row for row in managed_rows if row.id != keep[0].id]
    remove_ids = [row.id for row in remove]
    removed_meta_ids = []
    keep_meta_ids = []
    detached_annotation_ids = []
    if remove_ids or keep[0].created_at != FIXED_TIME:
        meta_rows = (
            await execute_core_read(
                db,
                select(prediction_meta_row.id, prediction_meta_row.prediction_id).where(
                    prediction_meta_row.prediction_id.in_([*remove_ids, keep[0].id])
                ),
            )
        ).all()
        removed_meta_ids = [
            row.id for row in meta_rows if row.prediction_id in remove_ids
        ]
        keep_meta_ids = [row.id for row in meta_rows if row.prediction_id == keep[0].id]
    if remove_ids:
        detached_annotation_ids = list(
            (
                await execute_core_read(
                    db,
                    select(annotation_row.id).where(
                        annotation_row.parent_prediction_id.in_(remove_ids)
                    ),
                )
            ).scalars()
        )
    task_ids = [task.id for task in tasks.values()]
    unmanaged_count = await db.scalar(
        select(func.count())
        .select_from(prediction_row)
        .where(
            prediction_row.task_id.in_(task_ids),
            prediction_row.model_version.is_distinct_from(model_version),
        )
    )
    if unmanaged_count:
        raise ScreenshotSeedReconcileError(
            "image_demo contains non-seed predictions; rerun with --repair"
        )
    await screenshot_operations_command(
        db,
        ReconcileSeedPredictions(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            operation="reconcile_seed_predictions",
            project_id=project.id,
            task_ids=tuple(task_ids),
            predicted_task_id=predicted_task.id,
            keep_prediction_id=keep[0].id,
            remove_prediction_ids=tuple(sorted(remove_ids, key=str)),
            model_version=model_version,
            fixed_time=FIXED_TIME,
        ),
    )
    for row in remove:
        cached = db.identity_map.get(
            identity_key(Prediction, ident=(row.id, row.created_at))
        )
        if cached is not None:
            db.expunge(cached)
    for meta_id in removed_meta_ids:
        cached = db.identity_map.get(identity_key(PredictionMeta, ident=meta_id))
        if cached is not None:
            db.expunge(cached)
    for annotation_id in detached_annotation_ids:
        cached_annotation = db.identity_map.get(
            identity_key(Annotation, ident=annotation_id)
        )
        if cached_annotation is not None:
            set_committed_value(cached_annotation, "parent_prediction_id", None)
    if keep[0].created_at != FIXED_TIME:
        cached = db.identity_map.get(
            identity_key(Prediction, ident=(keep[0].id, keep[0].created_at))
        )
        if cached is not None:
            db.expunge(cached)
            make_transient(cached)
            set_committed_value(cached, "created_at", FIXED_TIME)
            make_transient_to_detached(cached)
            db.add(cached)
        for meta_id in keep_meta_ids:
            cached_meta = db.identity_map.get(
                identity_key(PredictionMeta, ident=meta_id)
            )
            if cached_meta is not None:
                set_committed_value(cached_meta, "prediction_created_at", FIXED_TIME)
                set_committed_value(cached_meta, "created_at", FIXED_TIME)
    for key, task in tasks.items():
        set_committed_value(task, "total_predictions", 1 if key == "predicted" else 0)


async def _reconcile_annotations(
    db: AsyncSession,
    project: Project,
    tasks: dict[str, Task],
    users: dict[str, uuid.UUID],
) -> None:
    managed_ids = {
        key: uuid.uuid5(ANNOTATION_NAMESPACE, f"{project.display_id}:{key}")
        for key in ("submitted", "review", "completed")
    }
    task_ids = [task.id for task in tasks.values()]
    existing = list(
        (
            await execute_core_read(
                db, select(annotation_row).where(annotation_row.task_id.in_(task_ids))
            )
        ).scalars()
    )
    unmanaged = [
        annotation
        for annotation in existing
        if annotation.id not in managed_ids.values()
    ]
    if unmanaged:
        raise ScreenshotSeedReconcileError(
            "image_demo contains non-seed annotations; rerun with --repair"
        )
    by_id = {annotation.id: annotation for annotation in existing}
    annotation_requests = []
    for index, (key, annotation_id) in enumerate(managed_ids.items()):
        task = tasks[key]
        annotation = by_id.get(annotation_id)
        annotation_requests.append(
            SeedAnnotation(
                id=annotation_id,
                task_id=task.id,
                user_id=users["annotator"],
                geometry={
                    "type": "bbox",
                    "x": 0.18 + index * 0.05,
                    "y": 0.45,
                    "w": 0.24,
                    "h": 0.2,
                },
                ground_truth=key == "completed",
                created_at=FIXED_TIME + timedelta(hours=3 + index),
            )
        )

    task_evidence = []
    for key, task in tasks.items():
        has_annotation = key in managed_ids
        annotation_contributors = (str(users["annotator"]),) if has_annotation else ()
        if task.status in {"review", "completed"}:
            review_round_id = uuid.uuid5(task.id, "screenshot-review-round")
            review_contributors = annotation_contributors
            review_submitter = users["annotator"]
        else:
            review_round_id = None
            review_contributors = None
            review_submitter = None
        task_evidence.append(
            SeedTaskEvidence(
                task_id=task.id,
                status=task.status,
                review_round_id=review_round_id,
                annotation_contributor_ids=annotation_contributors,
                review_contributor_ids=review_contributors,
                review_submitter_id=review_submitter,
            )
        )

    await screenshot_operations_command(
        db,
        ReconcileSeedAnnotations(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            operation="reconcile_seed_annotations",
            project_id=project.id,
            annotations=tuple(annotation_requests),
            tasks=tuple(task_evidence),
            annotator_id=users["annotator"],
            fixed_time=FIXED_TIME,
        ),
    )
    for annotation_request in annotation_requests:
        annotation = by_id.get(annotation_request.id)
        if annotation is None:
            continue
        set_committed_value(annotation, "task_id", annotation_request.task_id)
        set_committed_value(annotation, "project_id", project.id)
        set_committed_value(annotation, "user_id", annotation_request.user_id)
        set_committed_value(annotation, "source", "manual")
        set_committed_value(annotation, "annotation_type", "bbox")
        set_committed_value(annotation, "tool_unit_id", "bbox")
        set_committed_value(annotation, "class_name", "car")
        set_committed_value(annotation, "geometry", annotation_request.geometry)
        set_committed_value(
            annotation, "attributes", {"seed_managed_by": SEED_MANAGED_BY}
        )
        set_committed_value(annotation, "is_active", True)
        set_committed_value(annotation, "was_cancelled", False)
        set_committed_value(annotation, "ground_truth", annotation_request.ground_truth)
        set_committed_value(annotation, "created_at", annotation_request.created_at)
        set_committed_value(annotation, "updated_at", annotation_request.created_at)
    evidence_by_id = {evidence.task_id: evidence for evidence in task_evidence}
    for task in tasks.values():
        has_annotation = task.id in {item.task_id for item in annotation_requests}
        evidence = evidence_by_id[task.id]
        set_committed_value(task, "total_annotations", int(has_annotation))
        set_committed_value(task, "is_labeled", has_annotation)
        set_committed_value(
            task,
            "annotation_contributor_ids",
            list(evidence.annotation_contributor_ids or ()),
        )
        set_committed_value(task, "review_round_id", evidence.review_round_id)
        set_committed_value(
            task,
            "review_contributor_ids",
            list(evidence.review_contributor_ids)
            if evidence.review_contributor_ids is not None
            else None,
        )
        set_committed_value(task, "review_submitter_id", evidence.review_submitter_id)


async def _set_task_state(
    db: AsyncSession,
    project: Project,
    tasks: dict[str, Task],
    users: dict[str, uuid.UUID],
) -> None:
    specs = PROJECT_SPECS["image_demo"].tasks
    states = []
    for index, task_spec in enumerate(specs):
        task = tasks[task_spec.key]
        assigned_at = FIXED_TIME + timedelta(hours=1)
        submitted_at = FIXED_TIME + timedelta(hours=4)
        reviewed_at = FIXED_TIME + timedelta(hours=6)
        assignee_id = users[task_spec.assignee_key] if task_spec.assignee_key else None
        reviewer_id = users[task_spec.reviewer_key] if task_spec.reviewer_key else None
        state = SeedTaskState(
            task_id=task.id,
            status=task_spec.status,
            sequence_order=index,
            assignee_id=assignee_id,
            reviewer_id=reviewer_id,
            assigned_at=assigned_at if assignee_id else None,
            submitted_at=(
                submitted_at if task_spec.status in {"review", "completed"} else None
            ),
            reviewer_claimed_at=(
                submitted_at + timedelta(minutes=20)
                if task_spec.key in {"review", "completed"}
                else None
            ),
            reviewed_at=reviewed_at if task_spec.status == "completed" else None,
        )
        states.append(state)
    await screenshot_operations_command(
        db,
        SetImageSeedTaskState(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            operation="set_image_seed_task_state",
            project_id=project.id,
            tasks=tuple(states),
        ),
    )
    for state in states:
        task = next(value for value in tasks.values() if value.id == state.task_id)
        for field in (
            "status",
            "sequence_order",
            "assignee_id",
            "reviewer_id",
            "assigned_at",
            "submitted_at",
            "reviewer_claimed_at",
            "reviewed_at",
        ):
            set_committed_value(task, field, getattr(state, field))
        for field in (
            "reject_reason",
            "reject_reason_type",
            "skip_reason",
            "skipped_at",
        ):
            set_committed_value(task, field, None)


async def _sync_counters(
    db: AsyncSession,
    project: Project,
    batches: dict[str, TaskBatch],
    tasks: dict[str, Task],
) -> None:
    register_project_counter_projection(db, project.id)
    batch_requests = []
    batch_values = {}
    for key, batch in batches.items():
        batch_tasks = [task for task in tasks.values() if task.batch_id == batch.id]
        values = {
            "total_tasks": len(batch_tasks),
            "completed_tasks": sum(task.status == "completed" for task in batch_tasks),
            "review_tasks": sum(task.status == "review" for task in batch_tasks),
            "approved_tasks": (
                sum(task.status == "completed" for task in batch_tasks)
                if key == "completed"
                else 0
            ),
            "rejected_tasks": 0,
        }
        batch_values[batch.id] = values
        batch_requests.append(SeedBatchCounters(id=batch.id, batch_key=key))
    all_tasks = list(tasks.values())
    project.total_tasks = len(all_tasks)
    project.completed_tasks = sum(task.status == "completed" for task in all_tasks)
    project.review_tasks = sum(task.status == "review" for task in all_tasks)
    project.in_progress_tasks = sum(task.status == "in_progress" for task in all_tasks)
    project.batch_summary = {
        "total": len(batches),
        "assigned": sum(batch.annotator_id is not None for batch in batches.values()),
        "in_review": sum(batch.status == "reviewing" for batch in batches.values()),
    }
    await screenshot_operations_command(
        db,
        SyncSeedBatchCounters(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            operation="sync_seed_batch_counters",
            project_id=project.id,
            task_ids=tuple(task.id for task in tasks.values()),
            batches=tuple(batch_requests),
        ),
    )
    for batch_id, values in batch_values.items():
        batch = next(value for value in batches.values() if value.id == batch_id)
        for field, value in values.items():
            set_committed_value(batch, field, value)
    updated_rows = (
        await execute_core_read(
            db,
            select(batch_row.id, batch_row.updated_at).where(
                batch_row.id.in_([batch.id for batch in batches.values()])
            ),
        )
    ).all()
    updated_by_id = {row.id: row.updated_at for row in updated_rows}
    for batch in batches.values():
        set_committed_value(batch, "updated_at", updated_by_id[batch.id])


def _task_display_id(logical_key: str, index: int) -> str:
    project_key = PROJECT_SPECS[logical_key].display_id.removeprefix("P-")
    return f"T-SS-{project_key}-{index + 1:03d}"


async def _pin_screenshot_timestamps(
    db: AsyncSession,
    resources: dict[str, tuple[Project, Dataset, dict[str, Task]]],
    membership_writes: SeedMembershipWrites | None = None,
) -> None:
    # Flush counters, annotations and backend changes before explicit updates;
    # an ORM assignment before autoflush can be overwritten by onupdate=now().
    if membership_writes is None:
        await db.flush()
    else:
        await membership_writes.flush()
    for logical_key, (project, dataset, tasks) in resources.items():
        await pin_seed_project_timestamps(
            db, project, created_at=FIXED_TIME, updated_at=FIXED_TIME
        )
        await db.execute(
            update(Dataset)
            .where(Dataset.id == dataset.id)
            .values(created_at=FIXED_TIME, updated_at=FIXED_TIME)
        )
        task_timestamps = []
        for index, task_spec in enumerate(PROJECT_SPECS[logical_key].tasks):
            task = tasks[task_spec.key]
            created_at = FIXED_TIME + timedelta(minutes=index)
            updated_at = (
                task.reviewed_at or task.submitted_at or task.assigned_at or created_at
            )
            task_timestamps.append(
                SeedTaskTimestamp(
                    project_id=project.id,
                    project_display_id=PROJECT_SPECS[logical_key].display_id,
                    task_id=task.id,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        await screenshot_operations_command(
            db,
            PinSeedTaskTimestamps(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                operation="pin_seed_task_timestamps",
                tasks=tuple(task_timestamps),
            ),
        )
        for timestamp in task_timestamps:
            task = next(
                value for value in tasks.values() if value.id == timestamp.task_id
            )
            set_committed_value(task, "created_at", timestamp.created_at)
            set_committed_value(task, "updated_at", timestamp.updated_at)


async def finalize_screenshot_seed_timestamps(
    db: AsyncSession, *, asset_sha256: dict[str, str]
) -> None:
    """Pin visible timestamps after all seed stages, including backend binding."""
    resources = {}
    for logical_key in PROJECT_SPECS:
        resources[logical_key] = await _resolve_owned_resources(
            db,
            logical_key,
            adopt_keys=frozenset(),
            asset_sha256=asset_sha256[logical_key],
        )
    await _pin_screenshot_timestamps(db, resources)
    await db.commit()


async def prepare_screenshot_seed_media(
    db: AsyncSession, *, asset_sha256: dict[str, str]
) -> dict[str, int]:
    """Accept owner work, wait for current outputs, and verify screenshot media."""
    from aap_assets_images.contract import (
        ImageBackfillSelection,
    )
    from app.services.assets_images import image_gateway, image_policy
    from scripts.image_media_backfill import accept_image_backfill

    datasets = []
    for logical_key, spec in PROJECT_SPECS.items():
        if spec.data_type not in {"image", "video"}:
            continue
        project, dataset, _ = await _resolve_owned_resources(
            db,
            logical_key,
            adopt_keys=frozenset(),
            asset_sha256=asset_sha256[logical_key],
        )
        datasets.append(
            (project.owner_id, dataset.id, dataset.display_id, dataset.data_type)
        )

    await db.commit()

    media_rows = []
    for owner_id, dataset_id, display_id, data_type in datasets:
        item_rows = (
            await db.execute(
                select(DatasetItem.id, DatasetItem.file_name, DatasetItem.file_type)
                .where(DatasetItem.dataset_id == dataset_id)
                .order_by(DatasetItem.id)
            )
        ).all()
        media_rows.append(
            _ScreenshotMediaDataset(
                owner_id=owner_id,
                dataset_id=dataset_id,
                display_id=display_id,
                data_type=data_type,
                items=tuple(
                    _ScreenshotMediaItem(
                        item_id=row.id,
                        file_name=row.file_name,
                        file_type=row.file_type,
                    )
                    for row in item_rows
                ),
            )
        )

    # Release the read transaction before owner admission, polling and object HEADs.
    await db.commit()

    for dataset in media_rows:
        if dataset.data_type == "image":
            await accept_image_backfill(
                ImageBackfillSelection(
                    owner_kind="dataset_item",
                    dataset_id=dataset.dataset_id,
                    purpose="thumbnail",
                    only_missing=True,
                    accepted_before=datetime.now(UTC),
                ),
                operation_id=uuid.uuid4(),
                policy=image_policy(),
            )
        elif dataset.data_type == "video":
            from app.workers.media import _schedule_video_backfill

            await _schedule_video_backfill(str(dataset.dataset_id), uuid.uuid4())

    deadline = time.monotonic() + SCREENSHOT_MEDIA_WAIT_SECONDS
    image_sources: dict[uuid.UUID, Any] = {}
    video_sources: dict[uuid.UUID, Any] = {}
    pending_image = pending_video = False
    while True:
        image_sources.clear()
        video_sources.clear()
        pending_image = pending_video = False
        for dataset in media_rows:
            image_items = [item for item in dataset.items if item.file_type == "image"]
            video_items = [item for item in dataset.items if item.file_type == "video"]
            if len(image_items) + len(video_items) != len(dataset.items):
                unexpected = next(
                    item.file_type
                    for item in dataset.items
                    if item.file_type not in {"image", "video"}
                )
                raise ScreenshotSeedReconcileError(
                    f"{dataset.display_id}: unexpected screenshot media type {unexpected}"
                )

            if image_items:
                from aap_assets_images.contract import (
                    ImageSourceSelector,
                    ReadImageSummariesRequest,
                )

                for offset in range(0, len(image_items), 500):
                    batch = image_items[offset : offset + 500]
                    async with image_gateway() as owner:
                        summaries = await owner.call(
                            "read_image_summaries",
                            ReadImageSummariesRequest(
                                actor_id=dataset.owner_id,
                                selectors=[
                                    ImageSourceSelector(dataset_item_id=item.item_id)
                                    for item in batch
                                ],
                                profile_version=image_policy().profile_version,
                            ),
                        )
                    for summary in summaries.items:
                        image_sources[summary.source.owner.id] = summary.source
                if any(
                    item.item_id not in image_sources
                    or not image_sources[item.item_id].thumbnail_current
                    or not image_sources[item.item_id].thumbnail_path
                    for item in image_items
                ):
                    pending_image = True

            if video_items:
                from aap_assets_media.contract import SummaryRequest
                from app.services.assets_media import media_gateway

                for offset in range(0, len(video_items), 500):
                    batch = video_items[offset : offset + 500]
                    async with media_gateway() as owner:
                        summaries = await owner.call(
                            "read_media_summaries",
                            SummaryRequest(item_ids=[item.item_id for item in batch]),
                        )
                    for summary in summaries.items:
                        video_sources[summary.dataset_item_id] = summary
                if any(
                    item.item_id not in video_sources
                    or not video_sources[item.item_id].thumbnail_path
                    or video_sources[item.item_id].source_drifted
                    or video_sources[item.item_id].materialized_generation
                    != video_sources[item.item_id].source_generation
                    or not (
                        video_sources[item.item_id].metadata.get("video") or {}
                    ).get("frame_count")
                    or not (
                        video_sources[item.item_id].metadata.get("video") or {}
                    ).get("fps")
                    or (video_sources[item.item_id].metadata.get("video") or {}).get(
                        "probe_error"
                    )
                    or (video_sources[item.item_id].metadata.get("video") or {}).get(
                        "poster_error"
                    )
                    or (video_sources[item.item_id].metadata.get("video") or {}).get(
                        "poster_frame_path"
                    )
                    != video_sources[item.item_id].thumbnail_path
                    for item in video_items
                ):
                    pending_video = True

        if not pending_image and not pending_video:
            break
        if time.monotonic() >= deadline:
            if pending_video:
                raise ScreenshotSeedReconcileError(
                    "screenshot video metadata/poster is not ready"
                )
            raise ScreenshotSeedReconcileError(
                "screenshot thumbnail/poster is not ready; check media generation "
                "errors or rerun with --repair"
            )
        await asyncio.sleep(2)

    item_count = 0
    for dataset in media_rows:
        for item in dataset.items:
            prefix = f"{dataset.display_id}: {item.file_name}"
            source = (
                image_sources.get(item.item_id)
                if item.file_type == "image"
                else video_sources.get(item.item_id)
            )
            thumbnail_path = source.thumbnail_path if source is not None else None
            head = (
                storage_service.verify_upload(
                    thumbnail_path, bucket=storage_service.media_cache_bucket
                )
                if thumbnail_path
                else None
            )
            if not head or head.get("ContentLength", 0) <= 0:
                raise ScreenshotSeedReconcileError(
                    f"{prefix}: screenshot thumbnail/poster is not ready; "
                    "check media generation errors or rerun with --repair"
                )
            item_count += 1
    return {"datasets": len(media_rows), "items": item_count}


async def reconcile_screenshot_seed(
    db: AsyncSession,
    *,
    preparation: ScreenshotSeedPreparation,
    asset_sha256: dict[str, str],
) -> dict[str, int]:
    users = await _seed_users(db)
    resources: dict[str, tuple[Project, Dataset, dict[str, Task]]] = {}
    for logical_key in PROJECT_SPECS:
        resources[logical_key] = await _resolve_owned_resources(
            db,
            logical_key,
            adopt_keys=preparation.adopt_keys,
            asset_sha256=asset_sha256[logical_key],
        )

    expected_task_ids = {
        _task_display_id(logical_key, index): tasks[task_spec.key].id
        for logical_key, (_, _, tasks) in resources.items()
        for index, task_spec in enumerate(PROJECT_SPECS[logical_key].tasks)
    }
    collisions = await execute_core_read(
        db,
        select(task_row.id, task_row.display_id).where(
            task_row.display_id.in_(expected_task_ids)
        ),
    )
    for task_id, display_id in collisions:
        if expected_task_ids[display_id] != task_id:
            raise ScreenshotSeedReconcileError(
                f"{display_id}: screenshot task display id is already used by another task"
            )

    axis_updates: list[tuple[uuid.UUID, str]] = []
    for logical_key, (project, dataset, tasks) in resources.items():
        register_seed_project_projection(db, project.id)
        project_state = PROJECT_STATE[logical_key]
        dataset_state = DATASET_STATE[logical_key]
        for field in (
            "name",
            "type_label",
            "type_key",
            "data_type",
            "ai_enabled",
            "ai_interactive_enabled",
        ):
            setattr(project, field, project_state[field])
        if "raster_mask_native_editing_enabled" in project_state:
            project.raster_mask_native_editing_enabled = project_state[
                "raster_mask_native_editing_enabled"
            ]
        project.owner_id = users[project_state["owner"]]
        project.status = "in_progress"
        project.tool_bindings = copy.deepcopy(project_state["tool_bindings"])

        dataset.name = dataset_state["name"]
        dataset.description = dataset_state["description"]
        dataset.data_type = dataset_state["data_type"]
        dataset.created_by = users[project_state["owner"]]
        if "axis_convention" in dataset_state:
            axis_updates.append((dataset.id, dataset_state["axis_convention"]))
        dataset.file_count = (
            await db.scalar(
                select(func.count())
                .select_from(DatasetItem)
                .where(DatasetItem.dataset_id == dataset.id)
            )
            or 0
        )
        reset_tasks = []
        for index, task_spec in enumerate(PROJECT_SPECS[logical_key].tasks):
            task = tasks[task_spec.key]
            reset_tasks.append(
                SeedTaskReset(
                    id=task.id,
                    file_path=task_spec.file_path,
                    display_id=_task_display_id(logical_key, index),
                    sequence_order=index,
                    status=task_spec.status,
                )
            )
        await screenshot_operations_command(
            db,
            ResetSeedTasks(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                operation="reset_seed_tasks",
                project_id=project.id,
                project_display_id=PROJECT_SPECS[logical_key].display_id,
                tasks=tuple(reset_tasks),
            ),
        )
        for reset in reset_tasks:
            task = next(value for value in tasks.values() if value.id == reset.id)
            for field, value in {
                "display_id": reset.display_id,
                "sequence_order": reset.sequence_order,
                "status": reset.status,
                "assignee_id": None,
                "reviewer_id": None,
                "batch_id": None,
                "assigned_at": None,
                "submitted_at": None,
                "reviewer_claimed_at": None,
                "reviewed_at": None,
                "reject_reason": None,
                "reject_reason_type": None,
                "skip_reason": None,
                "skipped_at": None,
                "total_annotations": 0,
                "total_predictions": 0,
                "is_labeled": False,
            }.items():
                set_committed_value(task, field, value)
        if logical_key != "image_demo":
            register_project_counter_projection(db, project.id)
            project.total_tasks = len(tasks)
            project.completed_tasks = 0
            project.review_tasks = 0
            project.in_progress_tasks = 0
            project.batch_summary = {}

    image_project, image_dataset, image_tasks = resources["image_demo"]
    membership_writes = await _reconcile_members(db, image_project, users)
    await membership_writes.flush_before_read()
    image_batches = await _reconcile_batches(
        db, image_project, image_dataset, image_tasks, users, membership_writes
    )
    await _set_task_state(db, image_project, image_tasks, users)
    await _reconcile_predictions(db, image_project, image_tasks)
    await _reconcile_annotations(db, image_project, image_tasks, users)
    await _sync_counters(db, image_project, image_batches, image_tasks)
    await _pin_screenshot_timestamps(db, resources, membership_writes)
    await db.flush()
    spatial = AssetsSpatialGateway(db)
    for dataset_id, axis_convention in axis_updates:
        await spatial.set_dataset_axis(
            SetDatasetAxisRequest(
                operation_id=uuid.uuid4(),
                dataset_id=dataset_id,
                axis_convention=axis_convention,
            ),
            same_session_native=True,
            commit=False,
        )
        dataset = next(
            dataset
            for _project, dataset, _tasks in resources.values()
            if dataset.id == dataset_id
        )
        await db.refresh(dataset, attribute_names=["metadata_"])
    await db.commit()
    return {
        "projects": len(resources),
        "tasks": sum(len(tasks) for _, _, tasks in resources.values()),
        "batches": len(image_batches),
    }
