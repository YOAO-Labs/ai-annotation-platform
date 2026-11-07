"""Desired-state ownership, rebuild, and task state for screenshot seed data."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient, make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.db.models.annotation import Annotation
from app.db.models.fixture_run import FixtureRunResource
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
from app.services.screenshot_seed_spec import (
    PROJECT_SPECS,
    SEED_MANAGED_BY,
    SEED_REVISION,
    USER_SPECS,
    fixture_persona_email,
    resolve_project_spec,
)
from yoao_assets_catalog.spatial_contract import SetDatasetAxisRequest
from app.services.assets_spatial import AssetsSpatialGateway
from yoao_labeling.screenshot_operations_contract import (
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
    run_id: uuid.UUID
    existing_keys: frozenset[str]


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


def _seed_marker(dataset: Dataset) -> dict:
    marker = (dataset.metadata_ or {}).get("seed")
    return marker if isinstance(marker, dict) else {}


async def _seed_users(db: AsyncSession, run_id: uuid.UUID) -> dict[str, uuid.UUID]:
    users: dict[str, uuid.UUID] = {}
    for key, (persona, role) in USER_SPECS.items():
        email = fixture_persona_email(run_id, persona)
        rows = await IdentityCommandService(db).lookup_seed_account_facts(email)
        resource = await db.scalar(
            select(FixtureRunResource).where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == "user",
                FixtureRunResource.logical_key == key,
                FixtureRunResource.state == "published",
            )
        )
        if (
            resource is None
            or len(rows) != 1
            or rows[0][0] != resource.resource_id
            or rows[0][2] != role
            or not rows[0][3]
        ):
            raise ScreenshotSeedReconcileError(
                f"{key}: exact active screenshot account with role {role} is required"
            )
        users[key] = rows[0][0]
    return users


async def prepare_screenshot_seed(
    db: AsyncSession, *, run_id: uuid.UUID
) -> ScreenshotSeedPreparation:
    """Reconcile only resources already recorded by this independent run."""
    from app.services import fixture_run_ownership as ownership

    run, root = await ownership.lock_original_scope(db, run_id)
    if run.id != root.id or root.family != "screenshot":
        raise ScreenshotSeedReconcileError("an open screenshot root is required")
    await _seed_users(db, run_id)
    resources = list(
        await db.scalars(
            select(FixtureRunResource).where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind.in_(("project", "dataset")),
            )
        )
    )
    by_key = {(resource.kind, resource.logical_key): resource for resource in resources}
    existing_keys: set[str] = set()
    for logical_key in PROJECT_SPECS:
        project = by_key.get(("project", logical_key))
        dataset = by_key.get(("dataset", logical_key))
        if (project is None) != (dataset is None):
            raise ScreenshotSeedReconcileError(
                f"{logical_key}: interrupted screenshot run requires exact cleanup"
            )
        if project is not None and dataset is not None:
            if project.state != "published" or dataset.state != "published":
                raise ScreenshotSeedReconcileError(
                    f"{logical_key}: screenshot resource publication is incomplete"
                )
            existing_keys.add(logical_key)
    if set(by_key) - {
        (kind, key) for key in PROJECT_SPECS for kind in ("project", "dataset")
    }:
        raise ScreenshotSeedReconcileError(
            "screenshot run contains unknown resource identities"
        )
    return ScreenshotSeedPreparation(
        run_id=run_id, existing_keys=frozenset(existing_keys)
    )


async def _resolve_owned_resources(
    db: AsyncSession,
    logical_key: str,
    *,
    run_id: uuid.UUID,
    asset_sha256: str,
) -> tuple[Project, Dataset, dict[str, Task]]:
    spec = PROJECT_SPECS[logical_key]
    resource_rows = list(
        await db.scalars(
            select(FixtureRunResource).where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind.in_(("project", "dataset")),
                FixtureRunResource.logical_key == logical_key,
                FixtureRunResource.state == "published",
            )
        )
    )
    resource_ids = {resource.kind: resource.resource_id for resource in resource_rows}
    if set(resource_ids) != {"project", "dataset"}:
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: exact manifest is incomplete"
        )
    project = await db.scalar(
        select(seed_projects).where(seed_projects.id == resource_ids["project"])
    )
    dataset = await db.get(Dataset, resource_ids["dataset"])
    if project is None or dataset is None:
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: owned project/dataset is missing"
        )
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
    if marker and (
        marker.get("managed_by") != SEED_MANAGED_BY
        or marker.get("profile") != "screenshots"
        or marker.get("logical_key") != logical_key
        or marker.get("run_id") != str(run_id)
    ):
        raise ScreenshotSeedReconcileError(
            f"{spec.dataset_display_id}: seed ownership marker does not match"
        )
    if marker and (
        marker.get("revision") != SEED_REVISION
        or marker.get("asset_sha256") != asset_sha256
    ):
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: screenshot seed revision or asset digest changed; "
            "clean up this exact run and start another"
        )
    dataset.metadata_ = {
        **(dataset.metadata_ or {}),
        "seed": {
            "managed_by": SEED_MANAGED_BY,
            "profile": "screenshots",
            "logical_key": logical_key,
            "run_id": str(run_id),
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
                    select(DatasetItem)
                    .join(
                        FixtureRunResource,
                        FixtureRunResource.resource_id == DatasetItem.id,
                    )
                    .where(
                        DatasetItem.dataset_id == dataset.id,
                        DatasetItem.metadata_["storage_relative_path"].astext
                        == task_spec.file_path.removeprefix(spec.storage_prefix),
                        FixtureRunResource.run_id == run_id,
                        FixtureRunResource.kind == "item",
                        FixtureRunResource.state == "published",
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
    run_id: uuid.UUID,
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
            f"{project.display_id}: unexpected batches {unexpected}; inspect this run's exact scope"
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
            run_id=run_id,
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
            run_id=run_id,
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
    db: AsyncSession, run_id: uuid.UUID, project: Project, tasks: dict[str, Task]
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
            "image_demo contains non-seed predictions; inspect this run's exact scope"
        )
    await screenshot_operations_command(
        db,
        ReconcileSeedPredictions(
            schema=SCREENSHOT_OPERATIONS_PROTOCOL,
            run_id=run_id,
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
    run_id: uuid.UUID,
    project: Project,
    tasks: dict[str, Task],
    users: dict[str, uuid.UUID],
) -> None:
    managed_ids = {
        key: uuid.uuid5(ANNOTATION_NAMESPACE, f"{project.display_id}:{key}")
        for key in ("submitted", "review", "completed")
    }
    from app.services import fixture_run_ownership as ownership

    new_resource_ids = []
    for key, annotation_id in managed_ids.items():
        resource = await db.get(
            FixtureRunResource, (run_id, "annotation", annotation_id)
        )
        logical_key = f"image_demo/{key}"
        if resource is None:
            await ownership.reserve(
                db, run_id, "annotation", annotation_id, logical_key=logical_key
            )
            new_resource_ids.append(annotation_id)
        elif resource.logical_key != logical_key or resource.state != "published":
            raise ScreenshotSeedReconcileError(
                f"{logical_key}: annotation ownership changed"
            )
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
            "image_demo contains non-seed annotations; inspect this run's exact scope"
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
            run_id=run_id,
            operation="reconcile_seed_annotations",
            project_id=project.id,
            annotations=tuple(annotation_requests),
            tasks=tuple(task_evidence),
            annotator_id=users["annotator"],
            fixed_time=FIXED_TIME,
        ),
    )
    for annotation_id in new_resource_ids:
        await ownership.publish_resource(db, run_id, "annotation", annotation_id)
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
    run_id: uuid.UUID,
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
            run_id=run_id,
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
    run_id: uuid.UUID,
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
            run_id=run_id,
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


def _task_display_id(project: Project, index: int) -> str:
    project_key = project.display_id.removeprefix("P-")
    return f"T-SS-{project_key}-{index + 1:03d}"


async def _pin_screenshot_timestamps(
    db: AsyncSession,
    run_id: uuid.UUID,
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
                    project_display_id=project.display_id,
                    task_id=task.id,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        await screenshot_operations_command(
            db,
            PinSeedTaskTimestamps(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                run_id=run_id,
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
    db: AsyncSession, *, run_id: uuid.UUID, asset_sha256: dict[str, str]
) -> None:
    """Pin visible timestamps after all seed stages, including backend binding."""
    resources = {}
    for logical_key in PROJECT_SPECS:
        resources[logical_key] = await _resolve_owned_resources(
            db,
            logical_key,
            run_id=run_id,
            asset_sha256=asset_sha256[logical_key],
        )
    await _pin_screenshot_timestamps(db, run_id, resources)
    await db.commit()


async def _screenshot_output_receipt(db, run_id, dataset, item, key):
    return await db.scalar(
        text(
            "SELECT aap_assets_v1.fixture_screenshot_output_receipt("
            ":run, :dataset, :item, :key)"
        ),
        {
            "run": run_id,
            "dataset": dataset.dataset_id,
            "item": item.item_id,
            "key": key,
        },
    )


async def _verify_screenshot_output(
    db, client, run_id, dataset, item, observed, proxy_origin
):
    from app.services.assets_images import image_gateway
    from app.services.assets_media import media_gateway
    from yoao_assets_images.contract import ReadImageSourceRequest
    from yoao_assets_media.contract import ReadSourceRequest

    receipt = await _screenshot_output_receipt(
        db, run_id, dataset, item, observed.thumbnail_path
    )
    await db.commit()
    fields = {
        "schema",
        "run_id",
        "dataset_id",
        "item_id",
        "key",
        "source_creation_id",
        "source_sha256",
        "record_id",
        "evidence_kind",
        "sha256",
        "size_bytes",
        "etag",
        "version_id",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != fields
        or receipt["schema"] != "aap-fixture-screenshot-output/v1"
        or receipt["run_id"] != str(run_id)
        or receipt["dataset_id"] != str(dataset.dataset_id)
        or receipt["item_id"] != str(item.item_id)
        or receipt["key"] != observed.thumbnail_path
        or receipt["evidence_kind"]
        != ("native" if item.file_type == "image" else "legacy")
        or type(receipt["size_bytes"]) is not int
        or receipt["size_bytes"] <= 0
        or not isinstance(receipt["etag"], str)
        or not receipt["etag"]
        or not isinstance(receipt["version_id"], str)
        or receipt["version_id"] in {"", "null"}
        or not isinstance(receipt["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is None
        or not isinstance(receipt["source_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["source_sha256"]) is None
    ):
        raise ScreenshotSeedReconcileError("screenshot output receipt is invalid")
    try:
        for field in ("source_creation_id", "record_id"):
            if str(uuid.UUID(receipt[field])) != receipt[field]:
                raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ScreenshotSeedReconcileError(
            "screenshot output receipt is invalid"
        ) from None

    if item.file_type == "image":
        async with image_gateway() as owner:
            reference = await owner.source_reference(
                ReadImageSourceRequest(
                    actor_id=dataset.owner_id, dataset_item_id=item.item_id
                )
            )
        source = reference.source
        url = reference.thumbnail_url
        if source.owner.kind != "dataset_item" or source.owner.id != item.item_id:
            raise ScreenshotSeedReconcileError("screenshot image source changed")
    else:
        async with media_gateway() as owner:
            reference = await owner.source_reference(
                ReadSourceRequest(
                    actor_id=dataset.owner_id, dataset_item_id=item.item_id
                )
            )
        source = reference.source
        url = reference.poster_url
        if source.dataset_item_id != item.item_id:
            raise ScreenshotSeedReconcileError("screenshot video source changed")
    if (
        source.dataset_id != dataset.dataset_id
        or source.source_drifted
        or source.source_generation != observed.source_generation
        or source.thumbnail_path != receipt["key"]
        or not reference.original_url
        or (
            isinstance(source.content_hash, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", source.content_hash) is not None
            and source.content_hash.lower() != receipt["source_sha256"]
        )
    ):
        raise ScreenshotSeedReconcileError("screenshot original source changed")
    try:
        parsed = urlsplit(url)
        expected = urlsplit(proxy_origin)
        valid_url = (
            parsed.scheme == expected.scheme == "http"
            and parsed.netloc == expected.netloc
            and parsed.username is None
            and parsed.password is None
            and parsed.path
            == f"/v1/fixture-native-output/{run_id}/{receipt['record_id']}"
            and not parsed.fragment
        )
    except (TypeError, ValueError, AttributeError):
        valid_url = False
    if not valid_url:
        raise ScreenshotSeedReconcileError("screenshot output proxy changed")
    digest = hashlib.sha256()
    size = 0
    async with client.stream("GET", url) as response:
        if (
            response.status_code != 200
            or response.headers.get("content-range") is not None
            or response.headers.get("etag") != receipt["etag"]
            or response.headers.get("x-amz-version-id") != receipt["version_id"]
        ):
            raise ScreenshotSeedReconcileError("screenshot output response changed")
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > receipt["size_bytes"]:
                raise ScreenshotSeedReconcileError("screenshot output size changed")
            digest.update(chunk)
    if size != receipt["size_bytes"] or digest.hexdigest() != receipt["sha256"]:
        raise ScreenshotSeedReconcileError("screenshot output bytes changed")
    current = await _screenshot_output_receipt(
        db, run_id, dataset, item, observed.thumbnail_path
    )
    await db.commit()
    if current != receipt:
        raise ScreenshotSeedReconcileError("screenshot output owner changed")


async def prepare_screenshot_seed_media(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    admission_id: uuid.UUID,
    asset_sha256: dict[str, str],
) -> dict[str, int]:
    """Accept owner work, wait for current outputs, and verify screenshot media."""
    from yoao_assets_images.contract import (
        ReadInternalImageWorkRequest,
        ImageBackfillSelection,
    )
    from app.services.assets_images import image_gateway, image_policy
    from app.db.models.fixture_run import FixtureNativeMediaWorkIntent
    from scripts.image_media_backfill import accept_image_backfill

    datasets = []
    for logical_key, spec in PROJECT_SPECS.items():
        if spec.data_type not in {"image", "video"}:
            continue
        project, dataset, _ = await _resolve_owned_resources(
            db,
            logical_key,
            run_id=run_id,
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
    image_work_intents: list[FixtureNativeMediaWorkIntent] = []
    video_outputs: list[tuple[str, uuid.UUID]] = []

    for dataset in media_rows:
        if dataset.data_type == "image":
            intent = FixtureNativeMediaWorkIntent(
                operation_id=uuid.uuid4(),
                family="image",
                run_id=run_id,
                admission_id=admission_id,
                dataset_id=dataset.dataset_id,
                state="prepared",
            )
            db.add(intent)
            await db.commit()
            accepted = await accept_image_backfill(
                ImageBackfillSelection(
                    owner_kind="dataset_item",
                    dataset_id=dataset.dataset_id,
                    purpose="thumbnail",
                    only_missing=True,
                    accepted_before=datetime.now(UTC),
                ),
                operation_id=intent.operation_id,
                policy=image_policy(),
            )
            intent.job_id = accepted.work.job_id
            intent.generation = accepted.work.generation
            intent.state = "admitted"
            await db.commit()
            image_work_intents.append(intent)
        elif dataset.data_type == "video":
            from app.services.content_snapshot import ContentError
            from app.services.fixture_media_execution import screenshot_producer_scope
            from app.services import video_derivative_originals as originals
            from app.services import video_derivatives as derivatives

            for item in dataset.items:
                source = await originals.pin(db, "dataset_item", item.item_id)
                authority = await originals.delegated(
                    db,
                    originals.system_authorization(
                        "dataset_item", item.item_id, source
                    ),
                )
                async with screenshot_producer_scope(
                    db, run_id, admission_id, dataset.dataset_id, item.item_id
                ) as producer:
                    output, needs_dispatch = await derivatives.prepare(
                        db, item.item_id, authority, "metadata"
                    )
                    output_id = output.id
                    if needs_dispatch:
                        prepared = await derivatives.prepare_dispatch(
                            db, [output_id], delivery_id=producer.delivery_id
                        )
                        await db.commit()
                        await producer.settle(output_id, prepared[0][0].delivery_id)
                    else:
                        await db.commit()
                        try:
                            await producer.reuse(output_id)
                        except ContentError as exc:
                            if exc.code == "fixture_media_reuse_unqualified":
                                exc.add_note(
                                    "Unqualified older screenshot output requires a fresh isolated run."
                                )
                            raise
                if needs_dispatch:
                    await derivatives.publish_dispatch(prepared)
                video_outputs.append((dataset.display_id, output_id))

    deadline = time.monotonic() + SCREENSHOT_MEDIA_WAIT_SECONDS
    image_sources: dict[uuid.UUID, Any] = {}
    video_sources: dict[uuid.UUID, Any] = {}
    pending_image = pending_video = False
    while True:
        image_sources.clear()
        video_sources.clear()
        pending_image = pending_video = False
        for intent in image_work_intents:
            async with image_gateway() as owner:
                status = await owner.call(
                    "read_internal_image_work",
                    ReadInternalImageWorkRequest(job_id=intent.job_id),
                )
            if status.work.generation != intent.generation:
                raise ScreenshotSeedReconcileError(
                    f"image backfill {intent.job_id}: owner generation changed"
                )
            if status.work.status in {"failed", "canceled"}:
                raise ScreenshotSeedReconcileError(
                    f"image backfill {intent.job_id}: {status.work.status}: {status.error_code}"
                )
            if status.work.status != "ready":
                pending_image = True
        if video_outputs:
            from app.db.models.video_derivative import VideoDerivativeOutput

            for display_id, output_id in video_outputs:
                output = await db.get(
                    VideoDerivativeOutput, output_id, populate_existing=True
                )
                if (
                    output is None
                    or output.state in {"retiring", "deleted"}
                    or output.error_code
                ):
                    code = output.error_code if output is not None else "missing"
                    raise ScreenshotSeedReconcileError(
                        f"{display_id}: video metadata owner did not finish: {code}"
                    )
                if output.state != "ready":
                    pending_video = True
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
                from yoao_assets_images.contract import (
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
                from yoao_assets_media.contract import SummaryRequest
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
        pending_receipt = False
        if not pending_image and not pending_video:
            for dataset in media_rows:
                for item in dataset.items:
                    source = (
                        image_sources[item.item_id]
                        if item.file_type == "image"
                        else video_sources[item.item_id]
                    )
                    if (
                        await _screenshot_output_receipt(
                            db, run_id, dataset, item, source.thumbnail_path
                        )
                        is None
                    ):
                        pending_receipt = True

        await db.commit()
        if not pending_image and not pending_video and not pending_receipt:
            break
        if time.monotonic() >= deadline:
            if pending_receipt:
                raise ScreenshotSeedReconcileError(
                    "screenshot output receipt did not settle; unqualified older "
                    "outputs require a fresh isolated run"
                )
            if pending_video:
                raise ScreenshotSeedReconcileError(
                    "screenshot video metadata/poster is not ready"
                )
            raise ScreenshotSeedReconcileError(
                "screenshot thumbnail/poster is not ready; check media generation "
                "errors and this run's recorded owner work"
            )
        await asyncio.sleep(2)

    reader = await db.scalar(
        text("SELECT aap_assets_v1.fixture_native_reader_identity(:run)"),
        {"run": run_id},
    )
    await db.commit()
    if not isinstance(reader, dict) or reader.get("state") != "active":
        raise ScreenshotSeedReconcileError("screenshot output reader is unavailable")
    try:
        origin = urlsplit(reader["proxy_origin"])
        valid_origin = (
            origin.scheme == "http"
            and bool(origin.hostname)
            and origin.port is not None
            and origin.username is None
            and origin.password is None
            and not origin.path
            and not origin.query
            and not origin.fragment
        )
    except (KeyError, TypeError, ValueError):
        valid_origin = False
    if not valid_origin:
        raise ScreenshotSeedReconcileError("screenshot output proxy is invalid")
    item_count = 0
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=False, trust_env=False
    ) as client:
        for dataset in media_rows:
            for item in dataset.items:
                source = (
                    image_sources[item.item_id]
                    if item.file_type == "image"
                    else video_sources[item.item_id]
                )
                await _verify_screenshot_output(
                    db, client, run_id, dataset, item, source, reader["proxy_origin"]
                )
                item_count += 1
    for intent in image_work_intents:
        intent.state = "settled"
    await db.commit()
    return {"datasets": len(media_rows), "items": item_count}


async def reconcile_screenshot_seed(
    db: AsyncSession,
    *,
    preparation: ScreenshotSeedPreparation,
    asset_sha256: dict[str, str],
) -> dict[str, int]:
    run_id = preparation.run_id
    users = await _seed_users(db, run_id)
    resources: dict[str, tuple[Project, Dataset, dict[str, Task]]] = {}
    for logical_key in PROJECT_SPECS:
        resources[logical_key] = await _resolve_owned_resources(
            db,
            logical_key,
            run_id=run_id,
            asset_sha256=asset_sha256[logical_key],
        )

    expected_task_ids = {
        _task_display_id(project, index): tasks[task_spec.key].id
        for logical_key, (project, _, tasks) in resources.items()
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
                    # The owner fences a reset on the resolved task scope; UUID
                    # storage adoption rewrites file_path away from the spec.
                    file_path=task.file_path,
                    display_id=_task_display_id(project, index),
                    sequence_order=index,
                    status=task_spec.status,
                )
            )
        await screenshot_operations_command(
            db,
            ResetSeedTasks(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                run_id=run_id,
                operation="reset_seed_tasks",
                project_id=project.id,
                project_display_id=project.display_id,
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
        db, run_id, image_project, image_dataset, image_tasks, users, membership_writes
    )
    await _set_task_state(db, run_id, image_project, image_tasks, users)
    await _reconcile_predictions(db, run_id, image_project, image_tasks)
    await _reconcile_annotations(db, run_id, image_project, image_tasks, users)
    await _sync_counters(db, run_id, image_project, image_batches, image_tasks)
    await _pin_screenshot_timestamps(db, run_id, resources, membership_writes)
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
