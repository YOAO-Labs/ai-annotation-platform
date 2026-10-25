"""Strict preflight contract for trusted LiDAR exports."""

from __future__ import annotations

import asyncio
import math
import uuid
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.services.project_access import get_workspace_project
from app.db.models.scene_track import SceneTrack
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from aap_assets_catalog.spatial_values import SensorCalibration
from app.schemas.export import (
    LidarExportIssue,
    LidarExportOptions,
    LidarExportPreflightResponse,
)
from aap_assets_catalog.axis_convention import R_NORM
from aap_workspace_contract import derive_classes_list
from app.services.labeling_scene import read_spatial_item_facts
from app.services.assets_spatial import AssetsSpatialGateway
from aap_assets_catalog.spatial_contract import (
    ReadPosesRequest,
    SpatialFailure,
    SpatialItem,
    normalize_calibration_record,
)
from app.services.storage import storage_service
from app.services.labeling_core_read_relations import (
    annotation_row,
    task_dataset_item_link_row,
    scene_track_row,
    task_row,
)


MAX_PREFLIGHT_ISSUES = 200
PREFLIGHT_LINK_CHUNK_SIZE = 1000
MAX_NUSCENES_EXPORT_FRAMES = 1000
MAX_NUSCENES_EXPORT_BOXES = 30_000
MAX_NUSCENES_POINT_BOX_TESTS = 100_000_000
MAX_NUSCENES_PCD_BYTES_PER_FRAME = 256 * 1024 * 1024
MAX_NUSCENES_PCD_BYTES_TOTAL = 4 * 1024 * 1024 * 1024
MAX_MULTICAMERA_COCO_IMAGES = 20_000
MAX_MULTICAMERA_COCO_ANNOTATIONS = 100_000
MAX_MULTICAMERA_COCO_MEDIA_BYTES = 20 * 1024 * 1024 * 1024
MAX_MULTICAMERA_COCO_MEDIA_BYTES_PER_IMAGE = 256 * 1024 * 1024


class LidarExportPreflightFailed(ValueError):
    def __init__(self, report: LidarExportPreflightResponse):
        self.report = report
        codes = sorted({issue.code for issue in report.issues})
        super().__init__(",".join(codes) or "lidar_export_preflight_failed")


def _rotation_determinant(matrix: list[float]) -> float:
    a, b, c = matrix[0], matrix[1], matrix[2]
    d, e, f = matrix[4], matrix[5], matrix[6]
    g, h, i = matrix[8], matrix[9], matrix[10]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _rotation_is_orthonormal(matrix: list[float]) -> bool:
    rows = (
        (float(matrix[0]), float(matrix[1]), float(matrix[2])),
        (float(matrix[4]), float(matrix[5]), float(matrix[6])),
        (float(matrix[8]), float(matrix[9]), float(matrix[10])),
    )
    for row in rows:
        if abs(sum(value * value for value in row) - 1.0) > 1e-3:
            return False
    return all(
        abs(sum(left[i] * right[i] for i in range(3))) <= 1e-3
        for index, left in enumerate(rows)
        for right in rows[index + 1 :]
    )


def _frame_key(task: Task, item) -> str:
    path = (task.file_path or "").lstrip("/")
    parts = path.split("/", 1)
    relative = parts[1] if len(parts) == 2 else path
    base = relative.rsplit(".", 1)[0]
    return base or task.display_id


def calibration_is_valid(raw: object) -> bool:
    try:
        calibration = SensorCalibration.model_validate(raw)
    except ValueError:
        return False
    values = [*calibration.extrinsic, *calibration.intrinsic]
    if calibration.rect:
        values.extend(calibration.rect)
    if not all(math.isfinite(float(value)) for value in values):
        return False
    extrinsic = calibration.extrinsic
    if abs(
        _rotation_determinant(extrinsic) - 1.0
    ) > 0.05 or not _rotation_is_orthonormal(extrinsic):
        return False
    if any(
        abs(float(extrinsic[index]) - expected) > 1e-6
        for index, expected in zip((12, 13, 14, 15), (0, 0, 0, 1), strict=True)
    ):
        return False
    intrinsic = calibration.intrinsic
    if float(intrinsic[0]) <= 1e-9 or float(intrinsic[4]) <= 1e-9:
        return False
    if (
        abs(float(intrinsic[6])) > 1e-9
        or abs(float(intrinsic[7])) > 1e-9
        or abs(float(intrinsic[8]) - 1.0) > 1e-9
    ):
        return False
    if calibration.rect and any(
        abs(float(calibration.rect[index]) - expected) > 1e-6
        for index, expected in zip((12, 13, 14, 15), (0, 0, 0, 1), strict=True)
    ):
        return False
    if calibration.rect and (
        abs(_rotation_determinant(calibration.rect) - 1.0) > 0.05
        or not _rotation_is_orthonormal(calibration.rect)
    ):
        return False
    return True


def _finite_vector(raw: object, length: int) -> bool:
    if not isinstance(raw, list) or len(raw) != length:
        return False
    try:
        return all(math.isfinite(float(value)) for value in raw)
    except (TypeError, ValueError):
        return False


def _quaternion_is_valid(raw: object) -> bool:
    if not _finite_vector(raw, 4):
        return False
    norm = math.hypot(*(float(value) for value in raw))
    return math.isfinite(norm) and norm > 1e-12


def _box_geometry_is_valid(raw: object) -> bool:
    if not isinstance(raw, dict) or raw.get("type") != "box_3d":
        return True
    center = raw.get("center")
    size = raw.get("size")
    rotation = raw.get("rotation")
    return (
        _finite_vector(center, 3)
        and _finite_vector(size, 3)
        and _finite_vector(rotation, 3)
        and all(float(value) > 0 for value in size)
    )


def _normalized_bbox_is_valid(raw: object) -> bool:
    if not isinstance(raw, dict) or raw.get("type") != "bbox":
        return False
    try:
        x, y, width, height = (float(raw[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        all(math.isfinite(value) for value in (x, y, width, height))
        and x >= 0
        and y >= 0
        and width > 0
        and height > 0
        and x + width <= 1 + 1e-9
        and y + height <= 1 + 1e-9
    )


def _source_scene_is_valid(raw: dict) -> bool:
    try:
        sample_count = int(raw.get("nbr_samples"))
    except (OverflowError, TypeError, ValueError):
        return False
    return (
        all(
            raw.get(field) not in (None, "") for field in ("token", "name", "log_token")
        )
        and isinstance(raw.get("description"), str)
        and sample_count > 0
        and all(
            raw.get(field) not in (None, "")
            for field in ("first_sample_token", "last_sample_token")
        )
    )


def _source_path_is_safe(raw: object) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    raw_parts = raw.split("/")
    path = PurePosixPath(raw)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in raw_parts)
        and "\\" not in raw
        and ":" not in raw
        and "\0" not in raw
    )


async def preflight_lidar_export(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    batch_id: uuid.UUID | None,
    targets: list[str],
    options: LidarExportOptions | None,
) -> LidarExportPreflightResponse:
    selected_role = options.kitti_camera_role if options else None
    issues: list[LidarExportIssue] = []
    issue_count = 0

    def add_issue(issue: LidarExportIssue) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_PREFLIGHT_ISSUES:
            issues.append(issue)

    needs_kitti = "kitti" in targets
    needs_nuscenes = "nuscenes" in targets
    needs_multicamera_coco = "coco-multicamera" in targets
    if not needs_kitti and not needs_nuscenes and not needs_multicamera_coco:
        return LidarExportPreflightResponse(
            ready=issue_count == 0,
            camera_roles=[],
            selected_camera_role=selected_role,
            checked_tasks=0,
            issue_count=issue_count,
            issues_truncated=issue_count > len(issues),
            issues=issues,
        )

    task_scope = [task_row.project_id == project_id]
    if batch_id is not None:
        task_scope.append(task_row.batch_id == batch_id)
    task_count = int(
        (await db.scalar(select(func.count(task_row.id)).where(*task_scope))) or 0
    )
    if needs_multicamera_coco and task_count == 0:
        add_issue(
            LidarExportIssue(
                code="multicamera_coco_export_empty",
                message="多相机 COCO 导出范围内没有任务",
            )
        )
    if needs_multicamera_coco and task_count > MAX_MULTICAMERA_COCO_IMAGES:
        add_issue(
            LidarExportIssue(
                code="multicamera_coco_export_too_large",
                message="多相机 COCO 导出任务数已超过 20,000 张相机图预算",
            )
        )
        return LidarExportPreflightResponse(
            ready=False,
            camera_roles=[],
            selected_camera_role=selected_role,
            checked_tasks=task_count,
            issue_count=issue_count,
            issues_truncated=False,
            issues=issues,
        )
    if needs_nuscenes:
        if task_count == 0:
            add_issue(
                LidarExportIssue(
                    code="nuscenes_export_empty",
                    message="nuScenes 导出范围内没有任务",
                )
            )
        elif task_count > MAX_NUSCENES_EXPORT_FRAMES:
            add_issue(
                LidarExportIssue(
                    code="nuscenes_export_too_large",
                    message=(
                        f"nuScenes 导出范围含 {task_count} 帧，"
                        f"当前上限为 {MAX_NUSCENES_EXPORT_FRAMES} 帧"
                    ),
                )
            )
        else:
            box_count_query = (
                select(func.count(annotation_row.id))
                .join(task_row, task_row.id == annotation_row.task_id)
                .where(
                    *task_scope,
                    annotation_row.geometry["type"].as_string() == "box_3d",
                    annotation_row.is_active.is_(True),
                    annotation_row.was_cancelled.is_(False),
                )
            )
            box_count = int((await db.scalar(box_count_query)) or 0)
            if box_count > MAX_NUSCENES_EXPORT_BOXES:
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_export_too_large",
                        message=(
                            f"nuScenes 导出范围含 {box_count} 个有效 3D 框，"
                            f"当前上限为 {MAX_NUSCENES_EXPORT_BOXES} 个"
                        ),
                    )
                )
        if issue_count:
            return LidarExportPreflightResponse(
                ready=False,
                camera_roles=[],
                selected_camera_role=selected_role,
                checked_tasks=task_count,
                issue_count=issue_count,
                issues_truncated=False,
                issues=issues,
            )

    task_query = (
        select(task_row)
        .where(task_row.project_id == project_id)
        .order_by(task_row.sequence_order, task_row.created_at, task_row.id)
    )
    if batch_id is not None:
        task_query = task_query.where(task_row.batch_id == batch_id)
    tasks = list((await db.execute(task_query)).scalars())
    tasks_by_id = {task.id: task for task in tasks}
    task_ids = [task.id for task in tasks]
    links_by_task: dict[uuid.UUID, dict[str, Any]] = {}
    camera_link_rows_by_task: dict[
        uuid.UUID, list[tuple[TaskDatasetItemLink, Any]]
    ] = {}
    camera_roles: set[str] = set()
    item_ids = {task.dataset_item_id for task in tasks if task.dataset_item_id}
    all_links: list[TaskDatasetItemLink] = []
    for start in range(0, len(task_ids), PREFLIGHT_LINK_CHUNK_SIZE):
        chunk_ids = task_ids[start : start + PREFLIGHT_LINK_CHUNK_SIZE]
        link_rows = list(
            (
                await db.execute(
                    select(task_dataset_item_link_row)
                    .where(task_dataset_item_link_row.task_id.in_(chunk_ids))
                    .order_by(task_dataset_item_link_row.role)
                )
            ).scalars()
        )
        for link in link_rows:
            all_links.append(link)
            item_ids.add(link.dataset_item_id)
            if link.role.startswith("camera_"):
                camera_roles.add(link.role)
    spatial_items, scenes_by_id = await read_spatial_item_facts(db, list(item_ids))
    for link in all_links:
        item = spatial_items.get(link.dataset_item_id)
        if item is None:
            continue
        links_by_task.setdefault(link.task_id, {})[link.role] = item
        if link.role.startswith("camera_"):
            camera_link_rows_by_task.setdefault(link.task_id, []).append((link, item))
    task_rows = [
        (
            task,
            spatial_items.get(
                task.dataset_item_id
                or next(
                    (
                        item.id
                        for role, item in links_by_task.get(task.id, {}).items()
                        if role == "primary_lidar"
                    ),
                    None,
                )
            ),
        )
        for task in tasks
    ]

    if needs_kitti and selected_role is None:
        add_issue(
            LidarExportIssue(
                code="kitti_camera_required",
                message="KITTI 导出必须显式选择一条相机通道",
            )
        )
    elif needs_kitti and selected_role not in camera_roles:
        add_issue(
            LidarExportIssue(
                code="kitti_camera_role_not_found",
                message=f"项目中不存在相机通道 {selected_role}",
                camera_role=selected_role,
            )
        )

    if needs_multicamera_coco:
        project = await get_workspace_project(db, project_id=project_id)
        known_classes = set(
            derive_classes_list(project.tool_bindings) if project is not None else []
        )
        camera_image_count = sum(
            len(rows) for rows in camera_link_rows_by_task.values()
        )
        total_media_bytes = 0
        camera_assets: dict[uuid.UUID, tuple[SpatialItem, dict]] = {}
        for task in tasks:
            common = {
                "task_id": task.id,
                "task_display_id": task.display_id,
                "frame_key": _frame_key(task, None),
            }
            rows = camera_link_rows_by_task.get(task.id, [])
            if not rows:
                add_issue(
                    LidarExportIssue(
                        code="multicamera_coco_camera_missing",
                        message="当前任务没有关联相机图像",
                        **common,
                    )
                )
                continue
            seen_item_roles: dict[uuid.UUID, str] = {}
            for link, item in rows:
                previous_role = seen_item_roles.setdefault(item.id, link.role)
                if previous_role != link.role:
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_camera_link_duplicate",
                            message=(
                                f"同一图像同时绑定为 {previous_role} 与 {link.role}"
                            ),
                            camera_role=link.role,
                            **common,
                        )
                    )
                try:
                    file_size = int(item.file_size or 0)
                except (TypeError, ValueError):
                    file_size = 0
                total_media_bytes += max(file_size, 0)
                source_basename = PurePosixPath(item.file_path or "").name
                export_rel_path = f"images/{link.role}/{task.id}/{source_basename}"
                if (
                    item.file_type != "image"
                    or not item.file_path
                    or source_basename in {"", ".", ".."}
                    or not _source_path_is_safe(export_rel_path)
                    or not item.width
                    or item.width <= 0
                    or not item.height
                    or item.height <= 0
                    or file_size <= 0
                ):
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_camera_item_invalid",
                            message=f"相机通道 {link.role} 的媒体类型、路径、宽高或大小无效",
                            camera_role=link.role,
                            **common,
                        )
                    )
                if file_size > MAX_MULTICAMERA_COCO_MEDIA_BYTES_PER_IMAGE:
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_export_too_large",
                            message=f"相机通道 {link.role} 的单图大小超过 256 MiB",
                            camera_role=link.role,
                            **common,
                        )
                    )
                if item.file_path and _source_path_is_safe(export_rel_path):
                    camera_assets.setdefault(
                        item.id, (item, common | {"camera_role": link.role})
                    )

        manual_member_count = 0
        for start in range(0, len(task_ids), PREFLIGHT_LINK_CHUNK_SIZE):
            manual_member_count += int(
                (
                    await db.scalar(
                        select(func.count(annotation_row.id)).where(
                            annotation_row.task_id.in_(
                                task_ids[start : start + PREFLIGHT_LINK_CHUNK_SIZE]
                            ),
                            annotation_row.sensor_role.is_not(None),
                            annotation_row.source == "manual",
                            annotation_row.geometry["type"].as_string() == "bbox",
                            annotation_row.is_active.is_(True),
                            annotation_row.was_cancelled.is_(False),
                        )
                    )
                )
                or 0
            )
        multicamera_scope_too_large = (
            camera_image_count > MAX_MULTICAMERA_COCO_IMAGES
            or manual_member_count > MAX_MULTICAMERA_COCO_ANNOTATIONS
            or total_media_bytes > MAX_MULTICAMERA_COCO_MEDIA_BYTES
        )
        if multicamera_scope_too_large:
            add_issue(
                LidarExportIssue(
                    code="multicamera_coco_export_too_large",
                    message=(
                        "多相机 COCO 导出超过 20,000 张图、100,000 个框或 "
                        "20 GiB 媒体预算"
                    ),
                )
            )
        else:
            manual_members: list[Annotation] = []
            for start in range(0, len(task_ids), PREFLIGHT_LINK_CHUNK_SIZE):
                manual_members.extend(
                    list(
                        (
                            await db.execute(
                                select(annotation_row).where(
                                    annotation_row.task_id.in_(
                                        task_ids[
                                            start : start + PREFLIGHT_LINK_CHUNK_SIZE
                                        ]
                                    ),
                                    annotation_row.sensor_role.is_not(None),
                                    annotation_row.source == "manual",
                                    annotation_row.geometry["type"].as_string()
                                    == "bbox",
                                    annotation_row.is_active.is_(True),
                                    annotation_row.was_cancelled.is_(False),
                                )
                            )
                        ).scalars()
                    )
                )

            scene_track_ids = {
                member.scene_track_id
                for member in manual_members
                if member.scene_track_id is not None
            }
            ordered_scene_track_ids = sorted(scene_track_ids, key=str)
            scene_tracks: dict[uuid.UUID, SceneTrack] = {}
            for start in range(
                0, len(ordered_scene_track_ids), PREFLIGHT_LINK_CHUNK_SIZE
            ):
                tracks = (
                    await db.execute(
                        select(scene_track_row).where(
                            scene_track_row.id.in_(
                                ordered_scene_track_ids[
                                    start : start + PREFLIGHT_LINK_CHUNK_SIZE
                                ]
                            ),
                            scene_track_row.project_id == project_id,
                        )
                    )
                ).scalars()
                scene_tracks.update({track.id: track for track in tracks})
            parents_by_key: dict[tuple[uuid.UUID, uuid.UUID], list[Annotation]] = {}
            for start in range(
                0, len(ordered_scene_track_ids), PREFLIGHT_LINK_CHUNK_SIZE
            ):
                parents = (
                    await db.execute(
                        select(annotation_row)
                        .join(task_row, task_row.id == annotation_row.task_id)
                        .where(
                            *task_scope,
                            annotation_row.scene_track_id.in_(
                                ordered_scene_track_ids[
                                    start : start + PREFLIGHT_LINK_CHUNK_SIZE
                                ]
                            ),
                            annotation_row.geometry["type"].as_string() == "box_3d",
                            annotation_row.is_active.is_(True),
                            annotation_row.was_cancelled.is_(False),
                        )
                    )
                ).scalars()
                for parent in parents:
                    parents_by_key.setdefault(
                        (parent.task_id, parent.scene_track_id), []
                    ).append(parent)
            member_items = {
                member.sensor_dataset_item_id: camera_assets[
                    member.sensor_dataset_item_id
                ][0]
                for member in manual_members
                if member.sensor_dataset_item_id in camera_assets
            }
            try:
                calibration_records = await AssetsSpatialGateway(db).read_calibrations(
                    list(member_items)
                )
                if len(calibration_records) != len(member_items):
                    raise ValueError("camera calibration is missing")
                for record in calibration_records:
                    state = normalize_calibration_record(record)
                    if not calibration_is_valid(
                        state.calibration.model_dump(mode="json")
                    ):
                        raise ValueError("camera calibration is invalid")
            except (SpatialFailure, ValueError, TypeError):
                add_issue(
                    LidarExportIssue(
                        code="multicamera_coco_calibration_invalid",
                        message="人工相机框关联的当前相机标定无效",
                    )
                )

            for member in manual_members:
                task = tasks_by_id[member.task_id]
                common = {
                    "task_id": task.id,
                    "task_display_id": task.display_id,
                    "frame_key": _frame_key(task, None),
                    "camera_role": member.sensor_role,
                }
                linked_item = links_by_task.get(task.id, {}).get(
                    str(member.sensor_role)
                )
                if (
                    linked_item is None
                    or member.sensor_dataset_item_id != linked_item.id
                    or member.project_id != project_id
                    or member.scene_track_id is None
                    or not member.track_id
                    or member.sensor_visibility is None
                    or member.calibration_revision is None
                    or member.calibration_revision < 1
                    or not isinstance(member.calibration_digest, str)
                    or len(member.calibration_digest) != 64
                    or any(
                        char not in "0123456789abcdef"
                        for char in member.calibration_digest.lower()
                    )
                ):
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_member_context_invalid",
                            message=f"人工相机框 {member.id} 的传感器上下文不闭合",
                            **common,
                        )
                    )
                if not _normalized_bbox_is_valid(member.geometry):
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_bbox_invalid",
                            message=f"人工相机框 {member.id} 的归一化 bbox 无效",
                            **common,
                        )
                    )
                if member.class_name not in known_classes:
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_category_unknown",
                            message=f"人工相机框 {member.id} 的类别不在项目类别表中",
                            **common,
                        )
                    )
                parents = parents_by_key.get(
                    (member.task_id, member.scene_track_id), []
                )
                scene_track = scene_tracks.get(member.scene_track_id)
                parent_valid = (
                    len(parents) == 1
                    and parents[0].project_id == project_id
                    and parents[0].class_name == member.class_name
                    and parents[0].track_id == member.track_id
                    and scene_track is not None
                    and scene_track.project_id == project_id
                    and scene_track.retired_at is None
                    and scene_track.class_name == member.class_name
                    and scene_track.track_id == member.track_id
                )
                if not parent_valid:
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_parent_invalid",
                            message=f"人工相机框 {member.id} 缺少一致的活跃 3D 主成员",
                            **common,
                        )
                    )

        async def verify_camera_asset(
            item, common: dict
        ) -> tuple[Any, dict, dict | None]:
            head = await asyncio.to_thread(
                storage_service.verify_upload,
                item.file_path,
                storage_service.datasets_bucket,
            )
            return item, common, head

        asset_rows = [] if multicamera_scope_too_large else list(camera_assets.values())
        for start in range(0, len(asset_rows), 32):
            verified = await asyncio.gather(
                *[
                    verify_camera_asset(item, common)
                    for item, common in asset_rows[start : start + 32]
                ]
            )
            for item, common, head in verified:
                if head is None or int(head.get("ContentLength", -1)) != int(
                    item.file_size or -1
                ):
                    add_issue(
                        LidarExportIssue(
                            code="multicamera_coco_media_drift",
                            message="相机媒体不存在或对象大小与 DatasetItem 不一致",
                            **common,
                        )
                    )

    scene_ids = {
        item.scene_id
        for _task, item in task_rows
        if item is not None and item.scene_id is not None
    }
    poses_by_frame: dict[tuple[uuid.UUID, int], Any] = {}
    all_scene_tasks: dict[uuid.UUID, list[tuple[Task, Any]]] = {}
    if needs_nuscenes and scene_ids:
        gateway = AssetsSpatialGateway(db)
        poses_by_frame = {
            (scene_id, pose.frame_index): pose
            for scene_id in scene_ids
            for pose in await gateway.read_poses(
                ReadPosesRequest(actor_id=None, scene_id=scene_id)
            )
        }
        full_rows = (
            await db.execute(
                select(task_row, task_row.dataset_item_id)
                .where(task_row.project_id == project_id)
                .where(task_row.dataset_item_id.is_not(None))
            )
        ).all()
        full_items, scenes_by_id = await read_spatial_item_facts(
            db, [item_id for _task, item_id in full_rows]
        )
        scenes_by_id = {
            scene_id: scene
            for scene_id, scene in scenes_by_id.items()
            if scene_id in scene_ids
        }
        for full_task, item_id in full_rows:
            full_item = full_items.get(item_id)
            if full_item is None or full_item.scene_id not in scene_ids:
                continue
            all_scene_tasks.setdefault(full_item.scene_id, []).append(
                (full_task, full_item)
            )

    camera_items = [
        item
        for linked in links_by_task.values()
        for role, item in linked.items()
        if role.startswith("camera_")
    ]
    calibration_states = {}
    if (needs_kitti or needs_nuscenes) and camera_items:
        for record in await AssetsSpatialGateway(db).read_calibrations(
            [item.id for item in camera_items]
        ):
            try:
                calibration_states[record.dataset_item_id] = (
                    normalize_calibration_record(record)
                )
            except (SpatialFailure, ValueError, TypeError):
                continue

    invalid_boxes_by_task: dict[uuid.UUID, list[uuid.UUID]] = {}
    box_counts_by_task: dict[uuid.UUID, int] = {}
    if needs_nuscenes:
        for start in range(0, len(task_ids), PREFLIGHT_LINK_CHUNK_SIZE):
            chunk_ids = task_ids[start : start + PREFLIGHT_LINK_CHUNK_SIZE]
            annotations = (
                await db.execute(
                    select(annotation_row).where(
                        annotation_row.task_id.in_(chunk_ids),
                        annotation_row.is_active.is_(True),
                        annotation_row.was_cancelled.is_(False),
                    )
                )
            ).scalars()
            for annotation in annotations:
                if (
                    isinstance(annotation.geometry, dict)
                    and annotation.geometry.get("type") == "box_3d"
                ):
                    box_counts_by_task[annotation.task_id] = (
                        box_counts_by_task.get(annotation.task_id, 0) + 1
                    )
                if not _box_geometry_is_valid(annotation.geometry):
                    invalid_boxes_by_task.setdefault(annotation.task_id, []).append(
                        annotation.id
                    )

    asset_contracts: dict[str, tuple[int, str, dict, str]] = {}

    def check_asset(
        *,
        key: object,
        size: object,
        sha256: object,
        common: dict,
        kind: str,
    ) -> None:
        if not isinstance(key, str) or not key or not isinstance(sha256, str):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_source_asset_contract_missing",
                    message=f"{kind} 缺少原始对象路径、大小或 SHA-256",
                    **common,
                )
            )
            return
        try:
            expected_size = int(size)
        except (TypeError, ValueError):
            expected_size = -1
        normalized_sha256 = sha256.lower()
        if (
            expected_size < 0
            or len(normalized_sha256) != 64
            or any(char not in "0123456789abcdef" for char in normalized_sha256)
        ):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_source_asset_contract_missing",
                    message=f"{kind} 的原始资产指纹无效",
                    **common,
                )
            )
            return
        existing = asset_contracts.get(key)
        if existing is not None and existing[:2] != (
            expected_size,
            normalized_sha256,
        ):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_source_asset_path_collision",
                    message=f"{kind} 路径被不同资产指纹重复引用",
                    **common,
                )
            )
            return
        asset_contracts[key] = (expected_size, normalized_sha256, common, kind)

    point_box_tests = 0
    total_pcd_bytes = 0
    pcd_size_exceeded = False
    if needs_nuscenes:
        for task, primary_item in task_rows:
            if primary_item is None:
                continue
            try:
                pcd_bytes = max(int(primary_item.file_size), 0)
            except (TypeError, ValueError):
                pcd_bytes = 0
            total_pcd_bytes += pcd_bytes
            pcd_size_exceeded = (
                pcd_size_exceeded or pcd_bytes > MAX_NUSCENES_PCD_BYTES_PER_FRAME
            )
            box_count = box_counts_by_task.get(task.id, 0)
            if box_count == 0:
                continue
            raw_point_count = (primary_item.metadata or {}).get("point_count")
            try:
                metadata_point_count = max(int(raw_point_count), 0)
            except (TypeError, ValueError):
                metadata_point_count = 0
            point_count = max(
                metadata_point_count,
                max(int(primary_item.file_size or 0) // 12, 0),
            )
            point_box_tests += max(point_count, 0) * box_count
        if pcd_size_exceeded or total_pcd_bytes > MAX_NUSCENES_PCD_BYTES_TOTAL:
            add_issue(
                LidarExportIssue(
                    code="nuscenes_export_too_large",
                    message=(
                        "nuScenes 平台 PCD 单帧或总字节数超过当前单次"
                        "导出预算，请减少 Scene 范围"
                    ),
                )
            )
        if point_box_tests > MAX_NUSCENES_POINT_BOX_TESTS:
            add_issue(
                LidarExportIssue(
                    code="nuscenes_export_too_large",
                    message=(
                        "nuScenes 精确框内点统计超过当前单次计算预算，"
                        "请减少 Scene 或 3D 框数量"
                    ),
                )
            )

    selected_tasks_by_scene: dict[uuid.UUID, list[tuple[Task, Any]]] = {}
    for task, primary_item in task_rows:
        frame_key = _frame_key(task, primary_item)
        common = {
            "task_id": task.id,
            "task_display_id": task.display_id,
            "frame_key": frame_key,
        }
        for annotation_id in invalid_boxes_by_task.get(task.id, []):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_box_geometry_invalid",
                    message=f"3D 框 {annotation_id} 含非有限值或非正尺寸",
                    **common,
                )
            )
        if needs_kitti or needs_nuscenes:
            if primary_item is None:
                add_issue(
                    LidarExportIssue(
                        code="primary_lidar_missing",
                        message="任务缺少主点云数据项",
                        **common,
                    )
                )
            convention = (
                primary_item.axis_convention if primary_item is not None else None
            )
            if convention is None:
                add_issue(
                    LidarExportIssue(
                        code="axis_convention_missing",
                        message="主点云数据集缺少 axis_convention",
                        **common,
                    )
                )
            elif convention == "raw" or convention not in R_NORM:
                add_issue(
                    LidarExportIssue(
                        code="axis_convention_untrusted",
                        message=(
                            f"axis_convention={convention!s} 无法用于可信坐标反变换"
                        ),
                        **common,
                    )
                )
            elif needs_nuscenes and convention != "iso_8855":
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_axis_convention_invalid",
                        message="nuScenes ego 源必须使用 iso_8855 坐标约定",
                        **common,
                    )
                )

        if needs_kitti and selected_role is not None:
            camera_item = links_by_task.get(task.id, {}).get(selected_role)
            if camera_item is None:
                add_issue(
                    LidarExportIssue(
                        code="camera_frame_missing",
                        message=f"当前帧缺少相机通道 {selected_role}",
                        camera_role=selected_role,
                        **common,
                    )
                )
            else:
                if (
                    not camera_item.width
                    or camera_item.width <= 0
                    or not camera_item.height
                    or camera_item.height <= 0
                ):
                    add_issue(
                        LidarExportIssue(
                            code="camera_image_size_missing",
                            message=f"相机通道 {selected_role} 缺少有效图像宽高",
                            camera_role=selected_role,
                            **common,
                        )
                    )
                current_calibration = calibration_states.get(camera_item.id)
                if current_calibration is None or not calibration_is_valid(
                    current_calibration.calibration.model_dump(mode="json")
                ):
                    add_issue(
                        LidarExportIssue(
                            code="camera_calibration_invalid",
                            message=f"相机通道 {selected_role} 缺少有效内外参",
                            camera_role=selected_role,
                            **common,
                        )
                    )

        if not needs_nuscenes or primary_item is None:
            continue
        if primary_item.scene_id is None or primary_item.frame_index is None:
            add_issue(
                LidarExportIssue(
                    code="nuscenes_scene_frame_missing",
                    message="nuScenes 导出要求每个主点云归属 Scene 并有 frame_index",
                    **common,
                )
            )
            continue
        selected_tasks_by_scene.setdefault(primary_item.scene_id, []).append(
            (task, primary_item)
        )
        scene = scenes_by_id.get(primary_item.scene_id)
        if scene is None or scene.source_format != "nuscenes":
            add_issue(
                LidarExportIssue(
                    code="nuscenes_source_scene_invalid",
                    message="Scene 不是可信的 nuScenes 来源",
                    **common,
                )
            )
            continue
        scene_meta = scene.source_metadata or {}
        source_scene = scene_meta.get("nuscenes_export")
        if scene_meta.get("frame") != "ego" or not isinstance(source_scene, dict):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_ego_frame_required",
                    message="nuScenes 可信导出只允许 importer --frame ego 数据",
                    **common,
                )
            )
            continue
        source_scene_row = source_scene.get("scene")
        source_log = source_scene.get("log")
        source_map = source_scene.get("map")
        if not all(
            isinstance(value, dict)
            for value in (source_scene_row, source_log, source_map)
        ):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_scene_contract_missing",
                    message="Scene 缺少原始 scene / log / map 合同",
                    **common,
                )
            )
            continue
        if not _source_scene_is_valid(source_scene_row):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_scene_contract_invalid",
                    message="nuScenes scene 字段不完整或 nbr_samples 无效",
                    **common,
                )
            )
        if (
            any(
                source_log.get(field) in (None, "")
                for field in (
                    "token",
                    "logfile",
                    "vehicle",
                    "date_captured",
                    "location",
                )
            )
            or any(
                source_map.get(field) in (None, "")
                for field in ("token", "category", "filename")
            )
            or not _source_path_is_safe(source_map.get("filename"))
        ):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_log_map_contract_invalid",
                    message="nuScenes log / map 字段不完整",
                    **common,
                )
            )
        if source_scene_row.get("log_token") != source_log.get(
            "token"
        ) or source_log.get("token") not in (source_map.get("log_tokens") or []):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_log_map_reference_invalid",
                    message="scene / log / map 原始引用不闭合",
                    **common,
                )
            )
        check_asset(
            key=source_scene.get("map_storage_key"),
            size=source_scene.get("map_file_size"),
            sha256=source_scene.get("map_sha256"),
            common=common,
            kind="map",
        )
        check_asset(
            key=primary_item.file_path,
            size=primary_item.file_size,
            sha256=primary_item.content_hash,
            common=common,
            kind="platform_pcd",
        )

        pose = poses_by_frame.get((primary_item.scene_id, primary_item.frame_index))
        if (
            pose is None
            or pose.timestamp_us is None
            or not _finite_vector(pose.ego_translation, 3)
            or not _quaternion_is_valid(pose.ego_rotation)
        ):
            add_issue(
                LidarExportIssue(
                    code="nuscenes_frame_pose_missing",
                    message="当前帧缺少完整 ego pose 或 LiDAR 时钟",
                    **common,
                )
            )

        sensor_items = dict(links_by_task.get(task.id, {}))
        sensor_items.setdefault("primary_lidar", primary_item)
        lidar_count = 0
        channels: set[str] = set()
        for role, item in sensor_items.items():
            if role != "primary_lidar" and not role.startswith("camera_"):
                continue
            source = (item.metadata or {}).get("nuscenes_export")
            if not isinstance(source, dict):
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_sensor_contract_missing",
                        message=f"通道 {role} 缺少原始 nuScenes 上下文",
                        camera_role=role if role.startswith("camera_") else None,
                        **common,
                    )
                )
                continue
            sample = source.get("sample")
            sample_data = source.get("sample_data")
            calibrated = source.get("calibrated_sensor")
            sensor = source.get("sensor")
            ego_pose = source.get("ego_pose")
            if not all(
                isinstance(value, dict)
                for value in (sample, sample_data, calibrated, sensor, ego_pose)
            ):
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_sensor_contract_missing",
                        message=f"通道 {role} 原始表上下文不完整",
                        **common,
                    )
                )
                continue
            channel = str(sensor.get("channel") or "")
            modality = str(sensor.get("modality") or "")
            filename = str(sample_data.get("filename") or "")
            references_valid = (
                bool(sample.get("token"))
                and sample.get("scene_token") == source_scene_row.get("token")
                and isinstance(sample.get("timestamp"), int)
                and isinstance(sample.get("prev"), str)
                and isinstance(sample.get("next"), str)
                and bool(sample_data.get("token"))
                and sample_data.get("sample_token") == sample.get("token")
                and sample_data.get("calibrated_sensor_token")
                == calibrated.get("token")
                and calibrated.get("sensor_token") == sensor.get("token")
                and sample_data.get("ego_pose_token") == ego_pose.get("token")
                and sample_data.get("is_key_frame") is True
                and isinstance(sample_data.get("timestamp"), int)
                and sample_data.get("timestamp") == ego_pose.get("timestamp")
                and isinstance(sample_data.get("prev"), str)
                and isinstance(sample_data.get("next"), str)
                and bool(calibrated.get("token"))
                and _finite_vector(calibrated.get("translation"), 3)
                and _quaternion_is_valid(calibrated.get("rotation"))
                and isinstance(calibrated.get("camera_intrinsic"), list)
                and bool(sensor.get("token"))
                and bool(ego_pose.get("token"))
                and isinstance(ego_pose.get("timestamp"), int)
                and _finite_vector(ego_pose.get("translation"), 3)
                and _quaternion_is_valid(ego_pose.get("rotation"))
                and channel
                and modality in {"lidar", "camera"}
                and filename
                and bool(sample_data.get("fileformat"))
                and isinstance(sample_data.get("width"), int)
                and isinstance(sample_data.get("height"), int)
                and _source_path_is_safe(filename)
            )
            if not references_valid or channel in channels:
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_sensor_reference_invalid",
                        message=f"通道 {role} 的 token 引用、时钟或路径不闭合",
                        **common,
                    )
                )
            channels.add(channel)
            if modality == "lidar":
                lidar_count += 1
                try:
                    raw_size = int(source.get("source_file_size"))
                except (TypeError, ValueError):
                    raw_size = -1
                if (
                    sample_data.get("fileformat") != "pcd"
                    or not filename.endswith(".pcd.bin")
                    or raw_size % 20 != 0
                ):
                    add_issue(
                        LidarExportIssue(
                            code="nuscenes_lidar_media_invalid",
                            message="原始 LiDAR 必须是五通道 float32 .pcd.bin",
                            **common,
                        )
                    )
                if pose is not None and (
                    pose.timestamp_us != sample_data.get("timestamp")
                    or pose.ego_translation != ego_pose.get("translation")
                    or pose.ego_rotation != ego_pose.get("rotation")
                ):
                    add_issue(
                        LidarExportIssue(
                            code="nuscenes_frame_pose_drift",
                            message="SceneFramePose 与原始 LIDAR_TOP ego_pose 不一致",
                            **common,
                        )
                    )
            elif (
                not item.width
                or not item.height
                or int(sample_data.get("width") or 0) != item.width
                or int(sample_data.get("height") or 0) != item.height
            ):
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_camera_size_invalid",
                        message=f"相机通道 {role} 宽高与原始 sample_data 不一致",
                        camera_role=role,
                        **common,
                    )
                )
            if role.startswith("camera_"):
                baseline_digest = source.get("platform_calibration_digest")
                state = calibration_states.get(item.id)
                if state is None or state.digest != baseline_digest:
                    add_issue(
                        LidarExportIssue(
                            code="nuscenes_camera_calibration_drift",
                            message=f"相机通道 {role} 导入后标定已变更",
                            camera_role=role,
                            **common,
                        )
                    )
            check_asset(
                key=source.get("source_storage_key"),
                size=source.get("source_file_size"),
                sha256=source.get("source_sha256"),
                common=common,
                kind=role,
            )
        if lidar_count != 1:
            add_issue(
                LidarExportIssue(
                    code="nuscenes_lidar_channel_invalid",
                    message="每帧必须且只能有一个 LiDAR 关键帧",
                    **common,
                )
            )

    if needs_nuscenes:
        for scene_id, selected_rows in selected_tasks_by_scene.items():
            common = {
                "task_id": selected_rows[0][0].id,
                "task_display_id": selected_rows[0][0].display_id,
                "frame_key": _frame_key(selected_rows[0][0], selected_rows[0][1]),
            }
            all_rows = all_scene_tasks.get(scene_id, [])
            scene = scenes_by_id.get(scene_id)
            source = (
                (scene.source_metadata or {}).get("nuscenes_export")
                if scene is not None
                else None
            )
            source_scene = source.get("scene") if isinstance(source, dict) else None
            try:
                expected_count = int(source_scene["nbr_samples"])
            except (KeyError, OverflowError, TypeError, ValueError):
                expected_count = -1
            selected_ids = {task.id for task, _item in selected_rows}
            all_ids = {task.id for task, _item in all_rows}
            frame_indices = sorted(item.frame_index for _task, item in all_rows)
            if (
                expected_count < 1
                or selected_ids != all_ids
                or len(all_ids) != expected_count
                or any(
                    frame_index != index
                    for index, frame_index in enumerate(frame_indices)
                )
            ):
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_scene_incomplete",
                        message="导出范围必须包含 Scene 的 0..nbr_samples-1 全部帧",
                        **common,
                    )
                )
                continue
            ordered = sorted(all_rows, key=lambda row: int(row[1].frame_index))
            source_samples = [
                ((item.metadata or {}).get("nuscenes_export") or {}).get("sample")
                for _task, item in ordered
            ]
            tokens = [
                str(sample.get("token") or "") if isinstance(sample, dict) else ""
                for sample in source_samples
            ]
            if (
                not all(tokens)
                or tokens[0] != str(source_scene.get("first_sample_token") or "")
                or tokens[-1] != str(source_scene.get("last_sample_token") or "")
                or any(
                    not isinstance(sample, dict)
                    or str(sample.get("prev") or "")
                    != (tokens[index - 1] if index else "")
                    or str(sample.get("next") or "")
                    != (tokens[index + 1] if index + 1 < len(tokens) else "")
                    or sample.get("timestamp") is None
                    for index, sample in enumerate(source_samples)
                )
            ):
                add_issue(
                    LidarExportIssue(
                        code="nuscenes_sample_chain_invalid",
                        message="原始 sample 的首尾、prev / next 或时间戳不闭合",
                        **common,
                    )
                )

        async def verify_asset(
            key: str, contract: tuple[int, str, dict, str]
        ) -> tuple[str, tuple[int, str, dict, str], dict | None]:
            head = await asyncio.to_thread(
                storage_service.verify_upload,
                key,
                storage_service.datasets_bucket,
            )
            return key, contract, head

        asset_rows = list(asset_contracts.items())
        for start in range(0, len(asset_rows), 32):
            verified = await asyncio.gather(
                *[
                    verify_asset(key, contract)
                    for key, contract in asset_rows[start : start + 32]
                ]
            )
            for _key, (expected_size, sha256, common, kind), head in verified:
                metadata = (head or {}).get("Metadata") or {}
                if (
                    head is None
                    or int(head.get("ContentLength", -1)) != expected_size
                    or str(metadata.get("sha256") or "").lower() != sha256
                ):
                    add_issue(
                        LidarExportIssue(
                            code="nuscenes_source_asset_drift",
                            message=f"{kind} 原始对象不存在或与导入指纹不一致",
                            **common,
                        )
                    )

    return LidarExportPreflightResponse(
        ready=issue_count == 0,
        camera_roles=sorted(camera_roles),
        selected_camera_role=selected_role,
        checked_tasks=len(tasks),
        issue_count=issue_count,
        issues_truncated=issue_count > len(issues),
        issues=issues,
    )


async def assert_lidar_export_ready(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    batch_id: uuid.UUID | None,
    targets: list[str],
    options: LidarExportOptions | None,
) -> LidarExportPreflightResponse:
    report = await preflight_lidar_export(
        db,
        project_id=project_id,
        batch_id=batch_id,
        targets=targets,
        options=options,
    )
    if not report.ready:
        raise LidarExportPreflightFailed(report)
    return report
