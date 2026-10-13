"""Desired-state ownership, rebuild, and task state for screenshot seed data."""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.project_delete import delete_project_records
from app.services.screenshot_seed_spec import (
    PROJECT_SPECS,
    SEED_MANAGED_BY,
    SEED_REVISION,
    USER_SPECS,
    resolve_project_spec,
)
from app.services.storage import storage_service

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


async def _seed_users(db: AsyncSession) -> dict[str, User]:
    users: dict[str, User] = {}
    for key, (email, role) in USER_SPECS.items():
        rows = list(
            (await db.execute(select(User).where(User.email == email))).scalars()
        )
        if len(rows) != 1 or rows[0].role != role:
            raise ScreenshotSeedReconcileError(
                f"seed user {email} with role {role} is required"
            )
        users[key] = rows[0]
    return users


async def _assert_rebuild_ownership(
    db: AsyncSession,
    logical_key: str,
    project: Project | None,
    dataset: Dataset | None,
    users: dict[str, User],
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
            or project.owner_id != owner.id
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
        allowed_creators = {users["project_admin"].id, users["admin"].id}
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
            select(Project).where(Project.display_id == spec.display_id)
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
            await delete_project_records(db, project)
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
                select(Project).where(Project.display_id == spec.display_id)
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
                await db.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.dataset_item_id == item.id,
                    )
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
            await db.execute(
                select(Task.dataset_item_id)
                .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                .where(
                    Task.project_id == project.id,
                    DatasetItem.dataset_id == dataset.id,
                )
            )
        ).scalars()
    )
    if linked_task_item_ids != managed_item_ids:
        raise ScreenshotSeedReconcileError(
            f"{logical_key}: managed dataset contains unexpected or missing tasks"
        )
    return project, dataset, tasks


async def _reconcile_members(
    db: AsyncSession, project: Project, users: dict[str, User]
) -> None:
    assigned_by = users["project_admin"].id
    for user_key, role in (("annotator", "annotator"), ("reviewer", "reviewer")):
        user = users[user_key]
        member = await db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == user.id,
            )
        )
        if member is None:
            member = ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role=role,
                assigned_by=assigned_by,
                assigned_at=FIXED_TIME,
            )
            db.add(member)
        else:
            member.role = role
            member.assigned_by = assigned_by
            member.assigned_at = FIXED_TIME


async def _reconcile_batches(
    db: AsyncSession,
    project: Project,
    dataset: Dataset,
    tasks: dict[str, Task],
    users: dict[str, User],
) -> dict[str, TaskBatch]:
    spec = PROJECT_SPECS["image_demo"]
    expected_display_ids = {batch_spec.display_id for batch_spec in spec.batches}
    existing_display_ids = set(
        (
            await db.execute(
                select(TaskBatch.display_id).where(TaskBatch.project_id == project.id)
            )
        ).scalars()
    )
    unexpected = sorted(existing_display_ids - expected_display_ids)
    if unexpected:
        raise ScreenshotSeedReconcileError(
            f"{project.display_id}: unexpected batches {unexpected}; rerun with --repair"
        )
    batches: dict[str, TaskBatch] = {}
    for batch_spec in spec.batches:
        rows = list(
            (
                await db.execute(
                    select(TaskBatch).where(
                        TaskBatch.project_id == project.id,
                        TaskBatch.display_id == batch_spec.display_id,
                    )
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
            else TaskBatch(project_id=project.id, display_id=batch_spec.display_id)
        )
        batch.dataset_id = dataset.id
        batch.name = batch_spec.name
        batch.description = "screenshots seed managed batch"
        batch.status = batch_spec.status
        batch.priority = 50
        batch.annotator_id = annotator.id if annotator else None
        batch.reviewer_id = reviewer.id if reviewer else None
        batch.assigned_user_ids = [
            str(user.id) for user in (annotator, reviewer) if user is not None
        ]
        batch.created_by = users["project_admin"].id
        batch.created_at = FIXED_TIME
        batch.updated_at = FIXED_TIME
        if not rows:
            db.add(batch)
        batches[batch_spec.key] = batch
    await db.flush()

    task_specs = {task_spec.key: task_spec for task_spec in spec.tasks}
    for key, task in tasks.items():
        task.batch_id = batches[task_specs[key].batch_key].id
    return batches


async def _reconcile_predictions(
    db: AsyncSession, project: Project, tasks: dict[str, Task]
) -> None:
    model_version = f"screenshot-seed:{SEED_REVISION}"
    managed = list(
        (
            await db.execute(
                select(Prediction).where(
                    Prediction.project_id == project.id,
                    Prediction.model_version == model_version,
                )
            )
        ).scalars()
    )
    predicted_task = tasks["predicted"]
    keep = [
        prediction for prediction in managed if prediction.task_id == predicted_task.id
    ]
    if len(keep) != 1:
        raise ScreenshotSeedReconcileError(
            "image_demo.predicted: expected one managed imported prediction"
        )
    remove = [prediction for prediction in managed if prediction.id != keep[0].id]
    if remove:
        prediction_ids = [prediction.id for prediction in remove]
        await db.execute(
            update(Annotation)
            .where(Annotation.parent_prediction_id.in_(prediction_ids))
            .values(parent_prediction_id=None)
        )
        await db.execute(
            delete(PredictionMeta).where(
                PredictionMeta.prediction_id.in_(prediction_ids)
            )
        )
        await db.execute(delete(Prediction).where(Prediction.id.in_(prediction_ids)))

    task_ids = [task.id for task in tasks.values()]
    unmanaged_count = await db.scalar(
        select(func.count())
        .select_from(Prediction)
        .where(
            Prediction.task_id.in_(task_ids),
            or_(
                Prediction.model_version.is_(None),
                Prediction.model_version != model_version,
            ),
        )
    )
    if unmanaged_count:
        raise ScreenshotSeedReconcileError(
            "image_demo contains non-seed predictions; rerun with --repair"
        )
    prediction = keep[0]
    if prediction.created_at != FIXED_TIME:
        # created_at is a partition key referenced by PredictionMeta. Move this
        # owned fixture within the same transaction, temporarily clearing the
        # nullable FK component, then restoring it without losing import metadata.
        await db.execute(
            update(PredictionMeta)
            .where(PredictionMeta.prediction_id == prediction.id)
            .values(prediction_created_at=None)
        )
        prediction.created_at = FIXED_TIME
        await db.flush()
        await db.execute(
            update(PredictionMeta)
            .where(PredictionMeta.prediction_id == prediction.id)
            .values(prediction_created_at=FIXED_TIME, created_at=FIXED_TIME)
        )
    for key, task in tasks.items():
        task.total_predictions = 1 if key == "predicted" else 0


async def _reconcile_annotations(
    db: AsyncSession, project: Project, tasks: dict[str, Task], users: dict[str, User]
) -> None:
    managed_ids = {
        key: uuid.uuid5(ANNOTATION_NAMESPACE, f"{project.display_id}:{key}")
        for key in ("submitted", "review", "completed")
    }
    task_ids = [task.id for task in tasks.values()]
    existing = list(
        (
            await db.execute(select(Annotation).where(Annotation.task_id.in_(task_ids)))
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
    for index, (key, annotation_id) in enumerate(managed_ids.items()):
        task = tasks[key]
        annotation = by_id.get(annotation_id)
        if annotation is None:
            annotation = Annotation(id=annotation_id)
            db.add(annotation)
        annotation.task_id = task.id
        annotation.project_id = project.id
        annotation.user_id = users["annotator"].id
        annotation.source = "manual"
        annotation.annotation_type = "bbox"
        annotation.tool_unit_id = "bbox"
        annotation.class_name = "car"
        annotation.geometry = {
            "type": "bbox",
            "x": 0.18 + index * 0.05,
            "y": 0.45,
            "w": 0.24,
            "h": 0.2,
        }
        annotation.attributes = {"seed_managed_by": SEED_MANAGED_BY}
        annotation.is_active = True
        annotation.was_cancelled = False
        annotation.ground_truth = key == "completed"
        annotation.created_at = FIXED_TIME + timedelta(hours=3 + index)
        annotation.updated_at = annotation.created_at

    for key, task in tasks.items():
        has_annotation = key in managed_ids
        task.total_annotations = 1 if has_annotation else 0
        task.is_labeled = has_annotation


def _set_task_state(tasks: dict[str, Task], users: dict[str, User]) -> None:
    specs = PROJECT_SPECS["image_demo"].tasks
    for index, task_spec in enumerate(specs):
        task = tasks[task_spec.key]
        assigned_at = FIXED_TIME + timedelta(hours=1)
        submitted_at = FIXED_TIME + timedelta(hours=4)
        reviewed_at = FIXED_TIME + timedelta(hours=6)
        task.sequence_order = index
        task.status = task_spec.status
        task.assignee_id = (
            users[task_spec.assignee_key].id if task_spec.assignee_key else None
        )
        task.reviewer_id = (
            users[task_spec.reviewer_key].id if task_spec.reviewer_key else None
        )
        task.assigned_at = assigned_at if task.assignee_id else None
        task.submitted_at = (
            submitted_at if task.status in {"review", "completed"} else None
        )
        task.reviewer_claimed_at = (
            submitted_at + timedelta(minutes=20)
            if task_spec.key in {"review", "completed"}
            else None
        )
        task.reviewed_at = reviewed_at if task.status == "completed" else None
        task.reject_reason = None
        task.reject_reason_type = None
        task.skip_reason = None
        task.skipped_at = None


def _sync_counters(
    project: Project, batches: dict[str, TaskBatch], tasks: dict[str, Task]
) -> None:
    for key, batch in batches.items():
        batch_tasks = [task for task in tasks.values() if task.batch_id == batch.id]
        batch.total_tasks = len(batch_tasks)
        batch.completed_tasks = sum(task.status == "completed" for task in batch_tasks)
        batch.review_tasks = sum(task.status == "review" for task in batch_tasks)
        batch.approved_tasks = batch.completed_tasks if key == "completed" else 0
        batch.rejected_tasks = 0
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


def _task_display_id(logical_key: str, index: int) -> str:
    project_key = PROJECT_SPECS[logical_key].display_id.removeprefix("P-")
    return f"T-SS-{project_key}-{index + 1:03d}"


async def _pin_screenshot_timestamps(
    db: AsyncSession,
    resources: dict[str, tuple[Project, Dataset, dict[str, Task]]],
) -> None:
    # Flush counters, annotations and backend changes before explicit updates;
    # an ORM assignment before autoflush can be overwritten by onupdate=now().
    await db.flush()
    for logical_key, (project, dataset, tasks) in resources.items():
        for model, resource in ((Project, project), (Dataset, dataset)):
            await db.execute(
                update(model)
                .where(model.id == resource.id)
                .values(created_at=FIXED_TIME, updated_at=FIXED_TIME)
            )
        for index, task_spec in enumerate(PROJECT_SPECS[logical_key].tasks):
            task = tasks[task_spec.key]
            created_at = FIXED_TIME + timedelta(minutes=index)
            updated_at = (
                task.reviewed_at or task.submitted_at or task.assigned_at or created_at
            )
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(created_at=created_at, updated_at=updated_at)
            )


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
    """Synchronously prepare and verify media only in owned screenshot datasets."""
    from app.workers.media import _backfill_media

    datasets = []
    for logical_key, spec in PROJECT_SPECS.items():
        if spec.data_type not in {"image", "video"}:
            continue
        _, dataset, _ = await _resolve_owned_resources(
            db,
            logical_key,
            adopt_keys=frozenset(),
            asset_sha256=asset_sha256[logical_key],
        )
        datasets.append(dataset)
    # The established media helpers use their own sessions.
    await db.commit()

    item_count = 0
    for dataset in datasets:
        await _backfill_media(str(dataset.id))
        items = list(
            (
                await db.execute(
                    select(DatasetItem)
                    .where(DatasetItem.dataset_id == dataset.id)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        for item in items:
            prefix = f"{dataset.display_id}: {item.file_name}"
            if item.file_type not in {"image", "video"}:
                raise ScreenshotSeedReconcileError(
                    f"{prefix}: unexpected screenshot media type {item.file_type}"
                )
            head = (
                storage_service.verify_upload(
                    item.thumbnail_path, bucket=storage_service.media_cache_bucket
                )
                if item.thumbnail_path
                else None
            )
            if not head or head.get("ContentLength", 0) <= 0:
                raise ScreenshotSeedReconcileError(
                    f"{prefix}: screenshot thumbnail/poster is not ready; "
                    "check media generation errors or rerun with --repair"
                )
            if item.file_type == "video":
                video = (item.metadata_ or {}).get("video") or {}
                if (
                    not video.get("frame_count")
                    or not video.get("fps")
                    or video.get("probe_error")
                    or video.get("poster_error")
                    or video.get("poster_frame_path") != item.thumbnail_path
                ):
                    raise ScreenshotSeedReconcileError(
                        f"{prefix}: screenshot video metadata/poster is not ready"
                    )
            item_count += 1
    return {"datasets": len(datasets), "items": item_count}


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
    collisions = await db.execute(
        select(Task.id, Task.display_id).where(Task.display_id.in_(expected_task_ids))
    )
    for task_id, display_id in collisions:
        if expected_task_ids[display_id] != task_id:
            raise ScreenshotSeedReconcileError(
                f"{display_id}: screenshot task display id is already used by another task"
            )

    for logical_key, (project, dataset, tasks) in resources.items():
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
        project.owner_id = users[project_state["owner"]].id
        project.status = "in_progress"
        project.tool_bindings = copy.deepcopy(project_state["tool_bindings"])

        dataset.name = dataset_state["name"]
        dataset.description = dataset_state["description"]
        dataset.data_type = dataset_state["data_type"]
        dataset.created_by = users[project_state["owner"]].id
        metadata = dict(dataset.metadata_ or {})
        if "axis_convention" in dataset_state:
            metadata["axis_convention"] = dataset_state["axis_convention"]
        dataset.metadata_ = metadata
        dataset.file_count = (
            await db.scalar(
                select(func.count())
                .select_from(DatasetItem)
                .where(DatasetItem.dataset_id == dataset.id)
            )
            or 0
        )
        for index, task_spec in enumerate(PROJECT_SPECS[logical_key].tasks):
            task = tasks[task_spec.key]
            task.display_id = _task_display_id(logical_key, index)
            task.sequence_order = index
            task.status = task_spec.status
            task.assignee_id = None
            task.reviewer_id = None
            task.batch_id = None
            task.assigned_at = None
            task.submitted_at = None
            task.reviewer_claimed_at = None
            task.reviewed_at = None
            task.reject_reason = None
            task.reject_reason_type = None
            task.skip_reason = None
            task.skipped_at = None
            task.total_annotations = 0
            task.total_predictions = 0
            task.is_labeled = False
        if logical_key != "image_demo":
            project.total_tasks = len(tasks)
            project.completed_tasks = 0
            project.review_tasks = 0
            project.in_progress_tasks = 0
            project.batch_summary = {}

    image_project, image_dataset, image_tasks = resources["image_demo"]
    await _reconcile_members(db, image_project, users)
    image_batches = await _reconcile_batches(
        db, image_project, image_dataset, image_tasks, users
    )
    _set_task_state(image_tasks, users)
    await _reconcile_predictions(db, image_project, image_tasks)
    await _reconcile_annotations(db, image_project, image_tasks, users)
    _sync_counters(image_project, image_batches, image_tasks)
    await _pin_screenshot_timestamps(db, resources)
    await db.commit()
    return {
        "projects": len(resources),
        "tasks": sum(len(tasks) for _, _, tasks in resources.values()),
        "batches": len(image_batches),
    }
